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
PROMPT_VERSION = "2026-10-29"  # sinhrons ar test_runner_db.PROMPT_VERSION
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
- a FLOOR PLAN may be ANY image in the gallery (first, middle or last) — detect it by
  content (drawing with walls, dimensions, room numbers / m² labels), not by position;
  if several plans exist (several floors), use all of them

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
Class of the BUILDING (Raimonds 2026-10-27) — NOT of the unit's interior finish.
An A-class building can have a grey-finish / unfinished unit inside; a nicely renovated
unit in an old building is still B.

A — ONLY a new or recently built, purpose-built, representative office / business centre
    in a strong business location, with top-tier features. Benchmarks in Riga: Satekles
    business centre, Novira Plaza, Verde, Elemental, Skanstes / Vienības-gatve new BCs,
    Teikums, Origo One, Place Eleven. Typical signs (several together): modern glass /
    curtain-wall facade, built roughly after 2008, BREEAM / LEED, professional
    management + reception, access control / turnstiles, several modern lifts,
    underground parking, central ventilation + cooling, named business centre.
    A new-build PROJECT of that kind (renders / under construction) in a prime location
    may be A.
B — everything decent but not that: older buildings (pre-war, Soviet, 1990s–2000s) even
    when renovated or with a renovated facade; "just nice" modern buildings that are not a
    landmark BC; small business parks; mixed-use residential buildings; ordinary
    renders of non-prime projects. Location alone does not make A.
C — visibly outdated, rough, neglected, unheated, technically weak buildings.
Industrial / warehouse buildings: A only for brand-new modern logistics / business-park
    buildings; otherwise B or C as before.
If facade / building cannot be judged -> unknown (do NOT default to A).


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
- unknown

### Logu_type
Allowed:
- Lielie Logi
- Standarta logi
- Maz logu
- Nav logi
- unknown

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

Meaning:
Electrical connection power available to the unit/object, in kilowatts (kW).

Output format (STRICT):
- a plain number string in kW, e.g. "63", "43.5", "150", "1000"
- no units, no "kW", no "~", no words, no ranges
- if nothing usable -> unknown

Primary:
- TEXT
Secondary:
- PHOTO only if a rating is clearly readable on the main switch / fuse / meter board

Recognize ALL of these text forms (Latvian / Russian / English):
1. Explicit kW: "jauda 150 kW", "elektrība 100 kW", "200KW", "pieslēgums 50 kW",
   "elektroapgāde 900 kW", "мощность 100 кВт", "power 60 kW" -> that number.
   MW -> multiply by 1000 ("1 MW" -> 1000, "0.2 MW" -> 200).
2. Amperes (connection / main fuse / input protection): "63A", "3x63A", "80 A pieslēgums",
   "ievadaizsardzība 100 A", "drošinātāji 250 A", "3 fāžu 50 A", "ввод 63А":
   convert to kW (commercial connections are 3-phase 400 V by default):
   kW = A x 0.69  (63 A -> 43.5, 80 A -> 55, 100 A -> 69, 250 A -> 172.5)
   Only if text clearly says single-phase ("1 fāze", "vienfāzes", "220 V", "230 V"):
   kW = A x 0.23.
   If text gives BOTH amperes and kW -> use the kW number as written.
   kVA (transformer / connection capacity, "630 kVA transformators"): kW = kVA x 0.9;
   if amperes are ALSO given, prefer the amperes conversion.
3. Several sub-units each with its own power ("40 kW veikals, 10 kW kafejnīca") -> sum
   of what is offered ("50").
4. Current + expandable ("110 kW, iespēja palielināt līdz 297 kW", "200 kW, iespējams
   palielināt līdz 1 MW") -> CURRENT value ("110", "200"), not the future one.
5. Photo: main breaker / fuse rating readable (e.g. "63A" printed on the main switch,
   "C63", "3x80A") -> convert with rule 2. Do not guess from the size of the board.

Do NOT use as power (these are NOT the electrical connection):
- heating boiler / furnace / heat pump / generator kW ("katls 50 kW", "kurtuve 50 kW",
  "siltumsūknis 12 kW", "ģenerators")
- solar station kW ("saules elektrostacija 30 kW", "saules paneļi")
- motor / equipment / compressor kW
- consumption or meter values in kWh, or electricity tariffs (EUR/kWh)
- voltage / phase info only: "380", "380 V", "400 V", "3 fāzes", "trīsfāzu", "380 kWh"
  (sellers write "380 kWh" meaning 380 V three-phase) -> this is NOT a power -> unknown
  unless a kW or A number is ALSO given.

Never estimate power from building type, area or use.

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

Meaning: parking available to the tenant / clients of THIS object.

Primary Source: TEXT
Secondary Source: PHOTO (IMG_TERRITORY / IMG_FACADE) — a REAL parking lot visible in
photos counts even if the text says nothing about parking.

Priority: TEXT beats PHOTO; the more specific statement beats the general one
("bezmaksas" / "par maksu" beats "pagalmā").

Definitions + signals:

1. Ir vietas bezmaksas — text says the object's own spaces are free:
   "bezmaksas stāvvieta", "bez maksas", "iekļauta nomas maksā / cenā", "free parking",
   "бесплатная парковка".
2. Ir vietas par maksu — text says own spaces exist for an extra fee:
   "par maksu", "par papildu samaksu", "maksas stāvvieta", "iespēja nomāt stāvvietu",
   "X EUR/mēn par vietu", "platная парковка".
3. Ir vietas — the object has its OWN spaces, price not stated. Text signals:
   "stāvvieta pagalmā", "slēgta / privāta / sava stāvvieta", "stāvvietas teritorijā",
   "pie ēkas", "stāvvietas klientiem", "N stāvvietas", "pazemes stāvvieta / garāža",
   "slēgta teritorija ar stāvlaukumu", "liels stāvlaukums".
   PHOTO signals (count even without text): marked parking bays at the building,
   a yard / fenced territory with a parking area behind gates, a parking lot on the
   object's territory, an underground garage entrance of the building.
4. Tikai ielas parking — only public / street parking: text says "ielas stāvvieta",
   "uz ielas", "stāvvieta pieejama tuvumā", "publiska / pilsētas stāvvieta blakus",
   "stāvvieta blakus ielā"; OR photos clearly show only a street frontage with no yard,
   territory or lot belonging to the object. This is a VALID value (typical city centre),
   not a defect.
5. Nav — text says there is no parking ("stāvvietas nav", "bez stāvvietas"), or text +
   photos clearly show a pedestrian zone / closed courtyard with no vehicle access.
6. unknown — text says nothing and photos show neither a lot, a yard, nor the street
   situation.

DO NOT infer from silence:
- "centrs" / "Rīgas centrs" / "Vecrīga" alone is NOT evidence of street-only parking.
- if text is silent and photos are only interiors -> unknown, NOT "Tikai ielas parking".

Traps (NOT evidence):
- cars parked on the street in a facade photo do NOT mean "Tikai ielas parking" by
  themselves (check for a yard / lot first)
- public transport stop, "velonovietne" (bike parking) are NOT parking
- one car in a yard photo without any marked bays or text is weak — prefer unknown
  unless the yard is clearly a parking area
- "garāža" at an autoservice is a work bay, not customer parking
- a loading yard with trucks = territory for logistics; parking only if bays / text say so
---

## Space_group
Meaning: what this UNIT / SPACE is best classified as based on ACTIVE use.

Primary:
- PHOTO (interior)
Secondary:
- TEXT

NON-PREMISES OBJECTS (Raimonds 2026-10-29) — decide this FIRST, before any premises group:
- "Zeme" = the object is a LAND PLOT / open territory / yard WITHOUT a building or premises
  being offered: field, meadow, forest plot, fenced asphalt / gravel yard for rent, cadastre
  maps with plot borders, drone photo with an outlined area, "zemes gabals", "teritorija",
  "laukums". Interior photos absent. Even if a shed or small building stands on it, when the
  offer is the land / yard -> Zeme. (Code then runs the dedicated land analysis.)
- "Investīciju objekts" = a WHOLE building or building complex offered as ONE asset: an entire
  hotel / guesthouse / sanatorium building, a station or manor complex, a whole office /
  industrial / mixed building with several tenants or units, "ēka", "ēku komplekss", "viesnīcas
  tipa ēka", "pārdod visu ēku". The listing is about the entire property, not one unit inside it.
  Potential_space_group = what the building could host. Investiciju_strategija must NOT be
  "Nav investīciju objekts" in this case.
- "Dzīvojamā" = a private house, apartment, residential building, summer house or similar that
  is offered as a RESIDENCE (or for hostel / apartment conversion) and has NO commercial
  fit-out: living rooms with wallpaper, bedrooms, bathrooms with tubs, kitchen of a home,
  "privātmāja", "dzīvoklis", "daudzdzīvokļu māja", "hostelim". NOT commercial premises at all.
  (Code removes such rows from the commercial pipeline.)

"unknown" is FORBIDDEN for commercial premises. If the object is premises inside a building,
you MUST choose a premises group — also for an EMPTY shell after renovation / unfinished
fit-out:
- use the uses named in the text in the order written (shop -> Tirdzniecība, office -> Birojs,
  cafe / restaurant -> Restorans/Cafe, warehouse -> Noliktava, production -> Ražošana);
- if the text names nothing: ground floor with street windows / shop front -> Tirdzniecība;
  upper-floor rooms / cabinets -> Birojs; gates, high ceilings, concrete floor -> Noliktava.
"unknown" is allowed ONLY when neither photos nor text show what the object is at all
(e.g. a single map image and no description).

Rules:
- If clear office interior -> Birojs
- If clear retail/shop/showcase logic -> Tirdzniecība
- If clear warehouse logic -> Noliktava
- If clear production logic -> Ražošana
- StockOfiss -> see definition below; use it ONLY when BOTH a pallet gate AND a shop-like
  customer entrance are visible / stated, on the 1st floor

StockOfiss (Raimonds 2026-10-27) = a SHOP + STOCK unit: one ground-floor space that
has BOTH (a) an overhead / sectional GATE through which pallets can be brought in, AND
(b) a SHOP-LIKE, presentable customer entrance (glass door / showroom front facing the
street or a public parking lot, signage possible), with high ceilings, so it can work as
a shop, a stock room and an office at once. A mezzanine / 2nd-level office is optional.
Typical: business-park units like "Ulbrokas 34a", "Grenču 5", "Plieņciema 5".
NOT StockOfiss:
- a warehouse / hall with an office but no shop-like customer entrance (only gates and a
  plain service door) -> Noliktava
- a unit whose main entrance is a loading RAMP / dock -> Noliktava
- a hangar / arch hall / rough industrial building, not presentable as a shop
- a unit inside a closed / fenced industrial territory with no public frontage
- a shop or office without a gate (pallets cannot be brought in) -> Tirdzniecība / Birojs
- any unit above the 1st floor

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
- An EMPTY but fitted-out production room counts too (Raimonds): tiled walls AND tiled /
  washable floor WITH floor drains AND an exhaust hood or ventilation ducts -> PVD, even
  if the equipment has been removed and even if the rest of the unit is an office hall.
  For such mixed units set Mazgajamas_sienas_check = checked (the production room has
  washable walls).
- SIZE DOES NOT MATTER (Raimonds): a 200 m² unit with an obvious ~20 m² production
  kitchen IS a PVD unit (cook in one part, store / pack in the rest). No minimum area.
- A kitchen WITHOUT a customer seating hall (no dining tables, no bar / serving counter
  facing guests) is PVD, not Restorans/Cafe. Use Restorans/Cafe only when a customer-
  facing hall is visible.
- Renovation potential alone does NOT qualify for PVD primary
- For renovation potential, use Potential_space_group instead

SPORTA ZĀLE (active gym/training use only):
- Use Sporta zāle ONLY if visible or text-supported ACTIVE gym/training use:
  - visible sports equipment (weight machines, dumbbells, training mats, boxing bags)
  - wall mirrors (key indicator for fitness/yoga/dance studios)
  - sports flooring (parquet, rubber, sports mats, gym-grade surface)
  - text mentions "sporta zāle", "fitness", "treniņi", "krossfit", "yoga", "pilates", "aerobika"

STUDIJA (active studio use only):
- Use Studija ONLY if the space is visibly fitted out / actively used as a studio:
  photo studio (backdrops, softboxes, cyclorama), dance / yoga / pilates studio
  (large hall with mirrors, barres, sprung or sports floor, no gym machines),
  beauty / cosmetology studio (salon chairs, mirrors, wash basins), recording or
  art studio
- A hall WITHOUT such fit-out is NOT active Studija -> classify by current use and
  put Studija into Potential_space_group if the HALL RULE below passes
- Do NOT use Studija for ordinary office rooms, cabinet-type layouts, narrow rooms,
  standard administrative premises

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

### Medicīna potential — GATES (Raimonds 2026-10-25; all must pass, mechanical)
Medicīna in Potential_space_group means "a medical code (MK 60) could realistically be
obtained here". Include it ONLY if ALL gates pass:
GATE 1 — Vides_pieejamiba_check = checked (positive step-free evidence; see that field).
   unknown or not checked -> NO Medicīna. Never infer accessibility from "1. stāvs".
GATE 2 — the unit: cik_WC shows ≥ 1 WC inside the unit (not shared), AND area_m2 ≥ 50,
   AND Space_condition is not "Kapitālais remonts".
GATE 3 — no hard physical veto: not a basement / half-basement, no internal stairs,
   doors not clearly narrow.
Water / sink: Ir_izlietne_telpa_check = checked OR plumbing extension plausible (WC in
   unit implies extendable plumbing) — supportive, not a gate.
Ventilation: checked OR feasible (modern / A-B class) — supportive, not a gate.
EXCEPTION — existing medical code: if TEXT or PHOTOS show an ACTIVE medical practice
   (dental chair, "stomatoloģija" / "klīnika" / "ārstu prakse" signage, med code 1264
   mentioned) -> include Medicīna even if accessibility fails, and mention in
   Debug_note: "esošs med kods — pārņemams tikai ar to pašu SIA / krēsla nomu".
Marketing text ("piemērots medicīnai") does NOT open any gate by itself.
CONSISTENCY: if Medicīna is in Potential_space_group, then Vides_pieejamiba_check must
be checked (or the existing-code exception applies). Otherwise remove Medicīna.

### PVD potential — decided by CODE GATES after the model (Raimonds 2026-10-27)
Your job: fill Gridas_materials, Mazgajamas_sienas_check and Ventilacijas_sistema_check
honestly — the code then decides PVD. For your own Potential_space_group, follow the
same logic:
- a WASHABLE FLOOR is mandatory (tiles, porcelain, epoxy, PU-cement, polished /
  hardened concrete, microcement, concrete with visible drains, rubber, linoleum,
  quartz vinyl). Raw / dusty / sandy concrete, carpet, laminate, wood, parquet -> no PVD.
- PLUS washable walls OR a ventilation system (the missing one can be added).
- a SHOP / unit inside a shopping centre needs washable floor AND washable walls — a
  tiled shop floor with mall ventilation is just a shop, not PVD.
- an office (Birojs / office building) above the 1st floor is never PVD; a warehouse or
  production space on an upper floor can be.
- text with PVD registration / food production / production kitchen -> PVD.
Marketing words ("piemērots pārtikai") alone do not qualify.

### Sporta zāle potential — decided by CODE GATES after the model (Raimonds 2026-10-27)
Fill Planojums, Lielaka_zale_m2 and Vides_pieejamiba_check honestly; the code decides.
Logic: the HALL must justify the whole unit — open layout or hall + small support rooms,
hall ≥ 100 m² AND ≥ 60 % of the unit area. A big labyrinth of cabinets with one hall is
NOT a gym. Plain warehouses / production halls / autoservices are NOT gym candidates.
Not accessible -> no gym.

### Other categories (existing rules)
StockOfiss (potential): include only if the unit already has BOTH a pallet gate and a
shop-like customer entrance (see the StockOfiss definition under Space_group), or one of
them is trivially present and the other is clearly stated as possible in the text. A plain
warehouse with a gate is NOT a StockOfiss candidate.

Restorans/Cafe:
- Allowed only if the space could realistically function as a restaurant,
  cafe, bar, or customer-facing food service unit
- Do not include Restorans/Cafe for an office kitchenette, staff kitchen
  corner, or simple built-in cabinets with sink inside office space
- If the visible kitchen element is only supportive to office use, keep
  Birojs and do not add Restorans/Cafe
- If the space is more suitable for food production / catering / prep
  without customer-facing hospitality use, prefer PVD

Studija — HALL RULE (Raimonds 2026-10-25):
Studija in Potential_space_group means: "this unit contains AT LEAST ONE real hall-type
room, so a dance / yoga / training / photo studio can be built here". The number of
other rooms does NOT matter — a 6-room unit with one big hall IS Studija; a 1-room
40 m² office is NOT.
- Include Studija ONLY if Lielaka_zale_m2 is a number ≥ 70 (exact or "~"), i.e. one
  undivided room of about 70 m² or more, shortest side roughly ≥ 6 m, usable by a
  group of people
- Supporting rooms (changing room, WC, reception, small cabinets, kitchenette) are
  a plus, never a minus
- The hall must be reachable without passing through another tenant's premises
- Daylight: hall has windows ("Lielie Logi" / "Standarta logi"); a basement hall
  without windows does NOT qualify
- Text signals that help: "zāle", "liela zāle", "atvērtā plānojuma", "open space",
  "viena liela telpa", "loft", "augsti griesti", "deju zāle", "treniņu zāle"
- Do NOT include Studija for: cabinet-style offices where every room < 70 m²; narrow
  shops (width ~4 m) even if long; an office with a 30 m² meeting room; warehouses /
  production halls with concrete floor and 4 m+ ceilings — those are Sporta zāle
  candidates, not Studija; rooms without windows
- If the biggest room size cannot be established (Lielaka_zale_m2 = unknown) -> do
  NOT include Studija
- Planojums helps when there is no plan: Planojums = "Atvērts" with unit area ≥ 70, or
  Planojums = "Zāle + palīgtelpas" with the estimated hall (see Lielaka_zale_m2 link
  rule) ≥ 70 -> the hall exists -> Studija. Planojums = "Kabinetu" or "Sekciju" -> no
  hall -> no Studija.

MANDATORY CONSISTENCY (hall rule is mechanical, not a judgement):
- If Lielaka_zale_m2 is a number ≥ 70 (exact or "~") AND Logu_type is not "Nav logi"
  AND the hall is not an industrial concrete-floor warehouse/production hall
  -> Potential_space_group MUST contain "Studija", even when Space_group is Birojs,
  Tirdzniecība, Medicīna or anything else, and even if the current use is a plain
  office. The "General fallback" below does NOT override this.
- If you did not include Studija, then Lielaka_zale_m2 must be < 70 or unknown.

Birojs as a POTENTIAL use of a warehouse / production / autoservice unit (Raimonds):
only if the WHOLE unit could become an office — an office / mixed building, normal
entrance, no industrial gates, not a basement. A small office room inside a warehouse
is an auxiliary room, NOT office potential.

Birojs as a POTENTIAL use of a SHOP (Tirdzniecība) (Raimonds 2026-10-27):
- a shop with a MEGA SHOPFRONT — floor-to-ceiling display windows / full glass storefront
  facing a street or mall, prime retail frontage -> keep it retail, do NOT add Birojs
- a ground-floor shop with ordinary / smaller windows, or in a non-central / quiet location
  -> Birojs is a good potential (showroom-office, e.g. an interior designer)

General fallback:
- If Space_group is clearly Birojs and there is no strong evidence for any
  other realistic use-case, return unknown — EXCEPT the mandatory Studija hall
  rule above (a ≥ 70 m² hall with windows always adds Studija)

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

Meaning (Raimonds 2026-10-26): can a tenant MOVE IN AND START WORKING with the furniture
that stays? Two answers only — there is NO "partly". Leftovers are NOT furniture.

Jā — the unit is FULLY fitted out as a working, lived-in space — what a broker would
honestly advertise as "mēbelēts" (Raimonds: if in doubt -> Nē). e.g.:
- office: EVERY main room is furnished as a finished office — workplaces filling the
  room, storage, meeting / lounge furniture, lamps / decor, it looks like a company works
  there or a turnkey office. A couple of desks with chairs standing in otherwise bare
  rooms (no storage, no decor, rooms look half-empty) is NOT furnished -> Nē.
  Text "mēbelēts", "ar mēbelēm", "aprīkots birojs", "furnished" -> Jā.
- cafe / restaurant: customer tables + chairs and service counter in place
- salon / medical / studio: working equipment and furniture in place for the activity
Nē — everything else, in particular:
- empty rooms
- leftovers: one desk + chair, a few loose chairs, an old cabinet, empty shelves,
  tables without chairs, items being moved out, boxes
- built-in kitchenette / kitchen cabinets, built-in wardrobes, radiators, blinds —
  these are fit-out, NOT furniture
- warehouse racks, workbenches, production equipment
- RENDERS / VISUALISATIONS / virtual staging (computer-generated images, "projekts",
  "vizualizācija", building under construction, "fit-out", "pelēkā apdare", "tiks
  nodots") — furniture in renders is not real -> Nē, even if the render is full of
  furniture. If ALL interior images are renders -> Nē.
Text beats photos: "bez mēbelēm" -> Nē; "mēbelēts" -> Jā (unless photos show an empty
space).
unknown — no interior photos and no text about furniture.

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

WHICH FLOOR (one value only):
- judge the floor of the MAIN WORKING SPACE(S) only — ignore kitchen / kitchenette,
  WC, bathroom, corridor, lobby, staircase, shared areas, balcony
- if main rooms have different floors -> the floor of the LARGEST main room; if they are
  about equal -> the floor seen in most interior photos
- condition does NOT change the material (worn linoleum is still "Linolejs"; damage
  belongs to Space_condition)
- "Betona grīda ar trapiem" ONLY if floor drains (trapi / grates) are actually visible or
  stated — never assume drains.
- a floor that is sand, gravel, dirt, crushed stone or a dusty unfinished base with no
  real floor finish -> "Betona grīda" at most (it is NOT a finished washable floor); if it
  is clearly not even concrete -> unknown

"PVC / vinils" is RETIRED — never output it. Choose between:
- Linolejs = soft PVC sheet flooring laid in one piece with welded seams; uniform or
  speckled / marbled pattern, often glossy, no plank / tile joints; typical in offices,
  clinics, schools, older commercial spaces; also small old PVC tiles
- Kvarca vinils = modern rigid vinyl (LVT / SPC / kvarcvinils) in separate PLANKS or
  TILES with visible joints, wood or stone imitation
Kvarca vinils vs Lamināts (wood-look planks look the same in photos):
- "Kvarca vinils" ONLY if the text says vinils / kvarcvinils / LVT / SPC, OR the same
  plank floor clearly continues into a kitchen / WC / wet area (laminate is not laid
  there); otherwise -> Lamināts
Paklājflīzes vs smooth floors:
- carpet tiles are MATT, textile surface, square tile grid, NO reflections
- if the floor reflects windows / lamps it is NOT carpet (Linolejs, Kvarca vinils,
  flīzes, slīpēts betons, epoksīds…)

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
- a car lift looks like: two posts (columns) with swing arms, four posts with drive-on
  runways, or a scissor platform the width of a car; often branded on the post
- NOT a car lift: inspection pit (bedre), paint / spray booth, tripod stands, work lamps,
  jack stands, trolley / transmission jacks, tyre changer, wheel balancer, shelving posts,
  a mobile INFRARED PAINT-DRYING LAMP (single column on a wheeled base with a lamp head /
  arm) — common in body shops and paint booths
- a single free-standing post with a lamp / tool head is not a car lift; a lift has a
  matching pair (or four) of posts on both sides of the car, or a platform under the car.
  A heavy steel post with an OVERHEAD CROSSBEAM running to a second post (even if the
  second post is partly outside the frame) IS a two-post car lift.
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

## Lielaka_zale_m2

Meaning:
Floor area (m²) of the LARGEST single undivided room in the unit — "the hall".
Used to decide Studija / Sporta zāle potential and to let clients filter by hall size.

Allowed output:
- exact number string in m², e.g. "120" — when the size is stated in text or readable
  on a plan
- approximate number string with ~, e.g. "~90" — when inferred from a plan without
  labels or from photos of an obviously large hall
- unknown — when no source gives confidence

Sources, in priority:
1. TEXT: room sizes listed ("zāle 120 m², ģērbtuve 15 m²", "telpa 85 kv.m",
   "divas telpas 60 + 25 m²") -> the biggest stated room. If the unit is clearly ONE
   room ("viena telpa", "atvērtā plānojuma", Cik_telpas = 1) -> the whole area.
2. PLAN image (any position in the gallery): read the m² label of the biggest room;
   if no labels, estimate from proportions vs the known total area (use ~).
3. PHOTOS: only when the hall is obviously large — visible wall to wall, a row of
   several windows along one wall, 5+ ceiling lights in rows, many desks / mats /
   people would fit. Estimate conservatively with ~ (e.g. "~80"). If unsure -> unknown.

Do NOT:
- return the total unit area when the unit is clearly divided into several rooms
- count corridors, WC, storage, kitchenette as the hall
- guess from the building type or from the listing category

Link with Planojums:
- Planojums = "Atvērts" -> Lielaka_zale_m2 = the whole unit area (exact, from area field)
- Planojums = "Zāle + palīgtelpas" and no text / plan size -> if total area ≥ 100 m²,
  estimate the hall as roughly 60 % of the total with "~" (e.g. 213 m² -> "~130");
  if total area < 100 m² -> unknown

---

## Skatlogi
Meaning: does the unit have a MEGA SHOPFRONT — floor-to-ceiling display windows / a full
glass storefront along most of its street (or mall / parking) frontage, at entrance
level? Prime retail frontage.
- Jā: big glass display windows from (near) floor to ceiling forming the facade of the
  unit, typical ground-floor shop / showroom frontage
- Nē: ordinary windows with a wall sill, small shop windows, upper floors, warehouses
- unknown: facade / frontage not visible
(Internal field used by the code; not stored.)

## Planojums

Meaning:
Physical LAYOUT TYPE of the unit — how the floor area is divided. A type, not a size,
so it can usually be read from ordinary photos even when no plan exists. Works for
all groups (office, shop, warehouse, production, medical).

Allowed values:
- Atvērts
- Zāle + palīgtelpas
- Kabinetu
- Sekciju
- unknown

Definitions:
- Atvērts = ONE undivided room is the whole unit; only WC / kitchenette / tiny entry
  besides it. Open-space office, shop hall, one warehouse hall, one production hall.
- Zāle + palīgtelpas = ONE dominant large room (a real hall: roughly ≥ 50 m² AND at
  least half of the unit area) PLUS several smaller rooms (changing room, cabinets,
  reception, storage, office corner). Krustpils-type: hall + 5 rooms; warehouse hall
  with an office block; shop hall with a back storage.
- Kabinetu = corridor + several small / medium rooms, none of them a real hall
  (biggest room < ~50 m² or less than half of the area). Cabinet office, clinic,
  salon with rooms, hotel-type. A 43 m² shop split into 4 rooms is Kabinetu, not
  "Zāle + palīgtelpas".
- Sekciju = several similar separate blocks, each with its own door / gate: storage
  boxes, autoservice bays, a row of production shops, self-storage.
- unknown = not enough evidence.

Sources, in priority:
1. PLAN image (any position) — read the layout directly.
2. TEXT: "atvērtā plānojuma", "open space", "viena liela telpa" -> Atvērts;
   "zāle un palīgtelpas", "liela zāle + 2 kabineti", "halle ar biroju" -> Zāle + palīgtelpas;
   "4 kabineti", "kabinetu sistēma", "koridors", "N atsevišķas telpas (no tām neviena
   dominējoša)" -> Kabinetu; "boksi", "sekcijas", "atsevišķas noliktavas ar vārtiem",
   "N boksi" -> Sekciju.
3. PHOTOS — count DISTINCT rooms (not photos!): the same room shot from 3 angles is
   ONE room (same windows, floor, walls, lights). Then:
   - one big room + only WC/kitchen -> Atvērts
   - one big room + several small rooms -> Zāle + palīgtelpas
   - only small/medium rooms + corridor with doors -> Kabinetu
   - repeated similar boxes / bays, each with own door or gate -> Sekciju
   - photos show only ONE room but the unit area is much larger than that room looks,
     and nothing shows what is behind the doors -> unknown (not Atvērts)

Do NOT infer from Space_group or building type. A warehouse can be Atvērts, Zāle +
palīgtelpas or Sekciju — decide from what is seen.

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

Meaning:
Telferis = a ceiling / beam / gantry mounted HOIST or overhead crane used to lift heavy goods
(electric chain hoist, monorail hoist, gantry crane, jib crane, "krāna sija", "kran-balka",
"konsoles celtnis"). Typical only in warehouse / production / workshop / heavy autoservice.
It is NOT a building elevator.

Allowed values:
- checked
- not checked
- unknown

Primary Source:
- PHOTO (IMG_INTERIOR / IMG_TECHNICAL)
Secondary Source:
- TEXT

Rules:
- checked ONLY if there is real hoist/crane evidence:
  * a ceiling/beam-mounted hoist or overhead crane is clearly visible in a photo, OR
  * CRANE RUNWAY visible: continuous crane rails / runway beams running along the
    columns high up on both sides of the hall, often with yellow crane-access platforms
    and ladders at rail height — the crane bridge may be outside the frame, the runway
    itself is the evidence, OR
  * text explicitly names an industrial hoist/crane: "telferis", "tālferis", "celtnis",
    "tilta celtnis", "krāna sija", "kran-balka", "konsoles celtnis", "liellaidu celtnis",
    "кран-балка", "тельфер", "overhead crane".
- TEXT WINS (Raimonds): if the listing text names a telferis / celtnis / krāna sija, it is
  checked — even if no photo shows it. There is no reason to doubt the seller here.
- the generic word "pacēlājs" / "kravas pacēlājs" / "pacelšanas iekārta" ALONE is NOT
  enough (Raimonds) — it is usually a freight lift, a scissor lift or a car lift.
- ventilation ducts, cable trays, sprinkler pipes, roof trusses, light rails, overhead
  sectional door tracks are NOT a hoist.
- the photo evidence must be in the OFFERED space; a crane in a neighbouring tenant's
  hall that is shown only for context does not count if the text/plan shows it is not
  part of the offer.
- DO NOT check for building elevators. The words "lifts", "kravas lifts", "pasažieru lifts",
  "kravas un pasažieru lifti", "Stāvs: X/Y/lifts" mean a passenger / freight ELEVATOR, which is a
  different building-level feature, NOT a telferis -> do NOT set checked based on these alone.
- a car service lift belongs to Auto_pacelajs_check, not here.
- if enough evidence exists and no hoist/crane is present -> not checked
- if evidence is insufficient -> unknown

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

Meaning: a mechanical air supply / exhaust system (ventilācija, rekuperācija, gaisa
apmaiņa). Cooling or heating alone is NOT ventilation.

checked — ONLY with SPECIFIC evidence:
- text mentions ventilation / rekuperācija / "pieplūdes-nosūces" / "gaisa apmaiņas
  sistēma" / "вентиляция" / "ventilation", OR
- photo shows AIR DUCTS: large round (spiral) or rectangular sheet-metal ducts with
  supply / exhaust grilles or diffusers along them, OR
- ventilation grilles / diffusers / slot diffusers set into a suspended ceiling, OR
- an air handling unit / rooftop unit / large wall exhaust fans of an industrial hall
  clearly visible.
NOT ventilation (do NOT check on these alone):
- sprinkler pipes (thin pipes with sprinkler heads, often red or grey)
- water / heating / gas pipes, district-heating mains along beams
- electrical cables, cable trays, lighting rails, crane beams / crane runways
- air heaters / gas infrared heaters / red heater boxes hanging under the roof
- a wall-mounted split air conditioner alone (that is cooling; a ceiling cassette AC
  counts only together with supply/exhaust grilles or ducts)
- "lots of engineering on the ceiling" in general — name the duct or grille you see
- ROUND RECESSED LAMPS / downlights / spotlights (shiny reflector, bulb inside, small
  round hole in the ceiling) — these are lights, NOT diffusers; a diffuser has visible
  slots, vanes, swirl blades or a perforated / grille face. A small round element in a
  suspended ceiling is a LAMP unless the slots / vanes are clearly visible. If you cannot
  tell lamp vs diffuser -> it is NOT evidence (do not check on it).
- for ceiling diffusers / grilles to count, at least TWO clearly identifiable
  (non-lamp) supply/exhaust outlets must be visible, or one plus a visible duct.
- concrete or steel BEAMS, girders, purlins, roof trusses, column boxings — structure,
  NOT rectangular ducts; a duct is thin sheet metal (galvanised / painted) with joints
  or flanges, hanging from the ceiling
- a single isolated pipe stub / old disused exhaust pipe / one elbow near a window —
  NOT a system; a ventilation system is a duct NETWORK running through the space with
  several grilles / outlets
not checked — enough interior photos show the ceiling / upper walls and none of the
  specific evidence above is present, and text does not mention ventilation.
unknown — ceiling / upper part of the space is not visible enough (e.g. only facade
  photos) and text is silent.

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
Meaning (Raimonds 2026-10-26): a sink installed IN THE WORKING ROOM ITSELF — a cabinet /
treatment room / salon / workshop area where the work is done (cosmetologist's cabinet,
hair-washing basin, dental / doctor's cabinet sink, lab sink, a sink on the wall of the
office / shop room). Needed by salons, medical cabinets, studios.

checked:
- photo shows a sink / wash basin mounted in a working room (next to a treatment table,
  salon chair, desk area, shop floor), or a hair-washing basin
- text: "izlietne kabinetā", "katrā kabinetā izlietne", "ūdens kabinetos", "izlietne telpā"
NOT counted (these do NOT make it checked):
- kitchen / kitchenette / tea-point sink (virtuve, virtuvīte)
- WC / toilet / bathroom / shower-room sink (sanmezgls, vannas istaba)
- shared / corridor sinks, utility / cleaning sinks in a technical room
- WAREHOUSE / PRODUCTION / AUTOSERVICE spaces: a small hand-wash or utility sink on a
  wall (in a side / technical room, by the gate, a tap on the hall wall) does NOT count.
  For these spaces check ONLY if the text says so, or there is a real work-water station
  (several sinks, stainless washing line, food-prep / production wash area).
not checked — enough interior photos of the working rooms, and the only sinks seen are
  in the kitchen / WC / bathroom (or none at all).
unknown — working rooms not shown well enough to tell.

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
Can a WHEELCHAIR USER get from the public street INTO the unit and to its WC WITHOUT
help — no steps anywhere on the route (or a ramp / lift where there are), doors wide
enough (≥ 90 cm), no level changes inside. This is the legal baseline for a medical
code (MK 60). Judge the ROUTE, not the floor number.

Primary:
- PHOTO: facade, entrance door, lobby / stairwell, interior level changes, plan
Secondary:
- TEXT: "lifts", "pandus", "vides pieejamība", "1. stāvs", "kāpnes", "puspagrabs",
  "cokolstāvs", "pagrabstāvs", "bēniņi", "mansards"

"1. stāvs" ALONE IS NOT EVIDENCE. Most Riga ground floors are raised 0.5–1.5 m above the
sidewalk and reached by steps. Only a visible or stated step-free route counts.

checked — ONLY with positive evidence of a step-free route. An EXTERIOR photo of the
actual entrance door (or explicit text) is REQUIRED; interior photos alone, a nice
modern interior or a modern facade without the entrance visible are NOT enough.
A modern residential/commercial building whose entrance sits on a podium with steps
and no visible ramp is "not checked".
- entrance door at sidewalk / ground level with no steps in front (threshold ≤ 1 small
  step) AND no level change seen inside, OR
- a ramp at the entrance (passenger ramp, not a loading ramp), OR
- a passenger lift visible or stated AND the lobby to the lift has no steps, OR
- a modern building (built after ~2000: glass facade, business centre, underground
  parking) where a lift is standard, AND no steps seen between street and lift
- text explicitly: "vides pieejamība", "pandus", "piekļuve cilvēkiem ratiņkrēslā", "lifts"

not checked — any ONE of these barriers seen or stated:
- steps / stairs at the entrance with no ramp (2 or more steps)
- a staircase inside the unit or in the lobby before any lift (split-level, mezzanine,
  spiral stairs, raised platform with steps)
- unit below street level: half-basement / basement — windows at or below sidewalk
  level, stairs leading DOWN from the entrance, small windows high under the ceiling
- raised ground floor: facade shows ground-floor window sills ~1 m or more above the
  sidewalk and no ramp / level entrance is visible
- floor 2 or higher in a historic / walk-up building with no lift evidence
- attic / mansard level (sloped ceilings, roof windows) without a lift stated
- entrance door clearly narrow (single-leaf old door < 80 cm) or the route is a
  narrow corridor with a turn a wheelchair cannot make
- text: "kāpnes", "bez lifta", "puspagrabs", "cokolstāvs", "pagrabs"

unknown — no entrance / facade / stairwell photo and no text about the route.
unknown means NOT accessible for any downstream decision.

Multi-level unit (ground floor + mezzanine / 2nd level): judge the ground-floor part;
if that part is step-free -> checked, and say "tikai 1. stāva daļa" in the reason.

Vides_pieejamiba_iemesls (reason) — ALWAYS fill, 1 short Latvian sentence citing what
you saw: e.g. "4 pakāpieni pie ieejas, pandusa nav (foto 2)", "ieeja ietves līmenī bez
pakāpieniem (foto 13), lifts vestibilā", "3. stāvs, lifts nav redzams, vēsturiska ēka",
"puspagrabs — logi ietves līmenī (foto 1)", "nav ieejas/fasādes foto". For unknown
say what is missing.

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

## NOMAS / IENĀKUMU EKSTRAKCIJA (TEXT-ONLY, STRICT)

These three fields are extracted STRICTLY from the listing TEXT (and any agent notes),
NEVER from photos and NEVER computed. A photo cannot prove that a lease exists or that
a number is true. If the text does not clearly state it, return the empty / unknown
value. Do NOT guess, do NOT infer from the price, do NOT estimate.

### ir_nomnieki  (are there existing tenants)
Allowed values: "ir", "nav", "unknown"
- "ir"  = the text CLEARLY states the property is currently leased / has an existing
  tenant (e.g. "iznomāts", "izīrēts", "ar esošu nomnieku", "spēkā esošs nomas līgums",
  "ar īrniekiem", "tenant in place").
- "nav" = the text CLEARLY states it is vacant / free / without tenants
  (e.g. "brīvs", "tukšs", "bez nomniekiem", "atbrīvots").
- "unknown" = the text says nothing clear about tenancy. This is the DEFAULT and the
  common case — most listings will be "unknown".
- Do NOT derive "ir" from the mere existence of a rental price. A "for rent" ad or an
  asking price is NOT proof of an existing tenant.

### investment_income  (existing rental income, from text)
- Extract ONLY if the text states a concrete rental income figure
  (e.g. "īres ienākumi 2000 EUR/mēnesī", "nomas maksa 1500 EUR").
- Copy the figure as written (keep the currency / period). Do NOT convert or compute.
- If there is no explicit income figure in the text -> "" (empty string).
- Never derive income from the sale price or the area.

### investment_yield  (yield / return %, from text)
- Extract ONLY if the text states a yield / return percentage
  (e.g. "atdeve 7%", "yield 8%", "ROI 6%", "fiksēta atdeve 6.5%").
- If the text calls it fixed / guaranteed, append " (fiksēta)" to the value.
- Copy as written. Do NOT compute yield from price and income yourself.
- If there is no explicit yield in the text -> "" (empty string).
- We deliberately do NOT compute a yield from price and income, because we cannot tell
  whether the stated income is from full or only partial occupancy.

### nomas_platiba_m2  (leased area, from text)
- Extract ONLY if the text states how much area is currently leased / rented out
  (e.g. "iznomāti 1200 m²", "daļēji iznomāts 500 m2", "leased 800 m²").
- Copy the number as written (keep m²). Do NOT compute, do NOT infer from the total area,
  do NOT assume full occupancy.
- If the text does not state a leased area -> "" (empty string).

NOTE: "der nomas tirgum" (fit for the rental market) is NOT a separate field — it is
already captured by Investiciju_strategija (anything other than "Nav investīciju
objekts" means a rentable commercial asset).

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
                "Zeme",
                "Investīciju objekts",
                "Dzīvojamā",
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
            "enum": ["Jā", "Nē", "unknown"]
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
        "Auto_pacelajs_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "street_entrance": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Cik_telpas": {"type": "string"},
        "Lielaka_zale_m2": {"type": "string"},
        "Planojums": {
            "type": "string",
            "enum": ["Atvērts", "Zāle + palīgtelpas", "Kabinetu", "Sekciju", "unknown"]
        },
        "Skatlogi": {"type": "string", "enum": ["Jā", "Nē", "unknown"]},
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
        "Vides_pieejamiba_iemesls": {"type": "string"},
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
        "ir_nomnieki": {
            "type": "string",
            "enum": ["ir", "nav", "unknown"]
        },
        "investment_income": {"type": "string"},
        "investment_yield": {"type": "string"},
        "nomas_platiba_m2": {"type": "string"},
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
        "street_entrance",
        "Auto_pacelajs_check",
        "Cik_telpas",
        "Lielaka_zale_m2",
        "Planojums",
        "Skatlogi",
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
        "Vides_pieejamiba_iemesls",
        "Mazgajamas_sienas_check",
        "Has_conference_room",
        "Zemes_gabals_m2",
        "Investiciju_strategija",
        "ir_nomnieki",
        "investment_income",
        "investment_yield",
        "nomas_platiba_m2",
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
    "Auto_pacelajs_check": "unknown",
    "street_entrance": "unknown",
    "Cik_telpas": "unknown",
    "Lielaka_zale_m2": "unknown",
    "Planojums": "unknown",
    "Skatlogi": "unknown",
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
    "Vides_pieejamiba_iemesls": "unknown",
    "Mazgajamas_sienas_check": "unknown",
    "Has_conference_room": "unknown",
    "Zemes_gabals_m2": "Nav minēts",
    "Investiciju_strategija": "unknown",
    "ir_nomnieki": "unknown",
    "investment_income": "",
    "investment_yield": "",
    "nomas_platiba_m2": "",
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
    # i.ss.LV un i.ss.COM (2026-09-24, Trikātas 4): ss.com spoguļa lapas bildes
    # servē no i.ss.com — bez tā «Caur linku» imports ss.com linkam redz 0 bildes.
    pattern = r'https://i\.ss\.(?:lv|com)/gallery/[^"\']+?\.jpg'
    found = re.findall(pattern, html, flags=re.IGNORECASE)

    if not found:
        pattern2 = r'//i\.ss\.(?:lv|com)/gallery/[^"\']+?\.jpg'
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



# ---------------------------------------------------------------------------
# MEDICĪNAS VĀRTI (Raimonds 2026-10-25/26) — deterministiski, PĒC AI.
# Modelis promptā vārdu «obligāti» apiet, tāpēc Medicīnu Potential_space_group
# galīgi nosaka kods. Identiska kopija: test_runner_db.py ↔ ai_text_helpers.py.
#   0) aktīva prakse (Space_group = Medicīna)  → paliek (esošs kods, piezīme)
#   1) ēkas profils owner_adapts_premises = False → NOŅEM vienmēr
#   2) ēkas profils owner_adapts_premises = True  → PIELIEK
#   3) citādi: pieejamība (AI vai bp.has_accessibility) = checked
#      ∧ WC telpā (ne koplietošanā) ∧ platība ≥ 50 ∧ ne kapitālais remonts
#      → PIELIEK, citādi NOŅEM. «unknown» pieejamība = NOŅEM.
# ---------------------------------------------------------------------------
MED = "Medicīna"


def _to_float(v):
    try:
        return float(str(v).replace(",", ".").replace("~", "").strip())
    except Exception:
        return None


def _wc_in_unit(cik_wc) -> bool:
    s = str(cik_wc or "").lower()
    if not s or s == "unknown" or "koplieto" in s or "shared" in s:
        return False
    return any(ch.isdigit() for ch in s) or "wc telpā" in s or "sanmezgl" in s


def fetch_building_flags(conn, building_profile_id):
    """{has_accessibility, owner_adapts_premises} no ēkas profila (None = nav zināms)."""
    if not building_profile_id:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_accessibility, owner_adapts_premises, has_fenced "
                "FROM properties.building_profiles WHERE id = %s",
                (building_profile_id,),
            )
            r = cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {}
    if not r:
        return {}
    if isinstance(r, dict):
        return {"has_accessibility": r.get("has_accessibility"),
                "owner_adapts_premises": r.get("owner_adapts_premises"),
                "has_fenced": r.get("has_fenced")}
    return {"has_accessibility": r[0], "owner_adapts_premises": r[1], "has_fenced": r[2]}


def apply_medicine_gates(result, area_m2=None, building_flags=None):
    """Maina result["Potential_space_group"] (Medicīna) + papildina Debug_note.
    Atgriež īsu iemeslu (str)."""
    bf = building_flags or {}
    pot_raw = str(result.get("Potential_space_group") or "")
    pot = [p.strip() for p in pot_raw.split(",")
           if p.strip() and p.strip().lower() != "unknown"]
    had = MED in pot

    access = str(result.get("Vides_pieejamiba_check") or "unknown")
    if bf.get("has_accessibility") is True:
        access = "checked"
    elif bf.get("has_accessibility") is False:
        access = "not checked"

    area = _to_float(area_m2) or _to_float(result.get("area_m2"))
    cond = str(result.get("Space_condition") or "")

    if str(result.get("Space_group") or "") == MED:
        want, why = True, "esošs med kods — pārņemams tikai ar to pašu SIA / krēsla nomu"
    elif str(result.get("Space_group") or "") in ("Noliktava", "Ražošana", "Autoserviss", "StockOfiss"):
        # Raimonds 2026-10-27: noliktavā/ražošanā med kods nevar būt vispār (arī ar ēkas pogu)
        want, why = False, f"{result.get('Space_group')} — med nekad"
    elif bf.get("owner_adapts_premises") is False:
        want, why = False, "īpašnieks NEpielāgo (ēkas profils)"
    elif bf.get("owner_adapts_premises") is True:
        want, why = True, "īpašnieks pielāgo (ēkas profils)"
    elif access != "checked":
        want, why = False, f"pieejamība={access}"
    elif not _wc_in_unit(result.get("cik_WC")):
        want, why = False, "nav WC telpā"
    elif area is None or area < 50:
        want, why = False, f"platība {area} < 50"
    elif cond == "Kapitālais remonts":
        want, why = False, "kapitālais remonts"
    else:
        want, why = True, "pieejams + WC telpā + ≥50 m²"

    if want and not had:
        pot.append(MED)
    if not want and had:
        pot = [p for p in pot if p != MED]
    result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"

    if want != had or str(result.get("Space_group") or "") == MED:
        note = str(result.get("Debug_note") or "")
        result["Debug_note"] = (note + f" | med: {why}")[:500]
    return why



# ---------------------------------------------------------------------------
# PVD VĀRTI (Raimonds 2026-10-27) — deterministiski, PĒC AI. Identiski abos repo.
#   0) aktīvs PVD (Space_group=PVD) vai PVD kods/pārtikas ražošana tekstā → paliek
#   1) ēkas profils owner_adapts_premises = False → NOŅEM;  = True → PIELIEK
#   2) birojs / biroju ēka virs 1. stāva → NOŅEM (noliktava/ražotne augšstāvā drīkst)
#   3) MAZGĀJAMA GRĪDA obligāta  ∧  (mazgājamas sienas  ∨  ventilācija) → PIELIEK, citādi NOŅEM
# ---------------------------------------------------------------------------
PVD = "PVD"
PVD_WASHABLE_FLOORS = {
    "Keramikas flīzes", "Porcelāna flīzes", "Epoksīda grīda", "Poliuretāna-cementa grīda",
    "Betona grīda ar trapiem", "Slīpēts betons", "Betons ar hardeneri", "Mikrocements",
    "Gumijas grīda", "Linolejs", "Kvarca vinils",
}
_PVD_TEXT_RE = None


def _floor_number(floor_raw):
    import re as _re
    m = _re.search(r"-?\d+", str(floor_raw or ""))
    return int(m.group(0)) if m else None


def apply_pvd_gates(result, floor_raw=None, building_flags=None, listing_text=""):
    import re as _re
    global _PVD_TEXT_RE
    if _PVD_TEXT_RE is None:
        _PVD_TEXT_RE = _re.compile(r"(PVD\s*(kod|reģistr|atļauj|aktīv)|pārtikas (ražošan|uzņēmum)|"
                                   r"ražošanas virtuv|cehs pārtik|food production)", _re.I)
    bf = building_flags or {}
    pot = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
           if p.strip() and p.strip().lower() != "unknown"]
    had = PVD in pot
    sg = str(result.get("Space_group") or "")
    btype = str(result.get("building_type") or "")
    floor_n = _floor_number(floor_raw)
    gfloor = str(result.get("Gridas_materials") or "")
    walls = str(result.get("Mazgajamas_sienas_check") or "") == "checked"
    vent = str(result.get("Ventilacijas_sistema_check") or "") == "checked"

    if sg == PVD or (listing_text and _PVD_TEXT_RE.search(listing_text)):
        want, why = True, "aktīvs PVD / PVD kods tekstā"
    elif bf.get("owner_adapts_premises") is False:
        want, why = False, "īpašnieks NEpielāgo (ēkas profils)"
    elif bf.get("owner_adapts_premises") is True:
        want, why = True, "īpašnieks pielāgo (ēkas profils)"
    elif (sg == "Birojs" or btype == "Biroju ēka") and floor_n is not None and floor_n > 1:
        want, why = False, f"birojs {floor_n}. stāvā"
    elif gfloor not in PVD_WASHABLE_FLOORS:
        want, why = False, f"grīda nav mazgājama ({gfloor or 'unknown'})"
    elif (sg == "Tirdzniecība" or btype == "Tirdzniecības centrs") and not walls:
        # Raimonds 2026-10-27 (Dzelzavas 74 «Mego»): veikala flīžu grīda + centra
        # ventilācija ≠ PVD — veikalam vajag arī mazgājamas sienas
        want, why = False, "veikals / tirdzn. centrs bez mazgājamām sienām"
    elif not (walls or vent):
        want, why = False, "nav ne mazgājamu sienu, ne ventilācijas"
    else:
        want, why = True, f"mazgājama grīda ({gfloor}) + {'sienas' if walls else 'ventilācija'}"

    if want and not had:
        pot.append(PVD)
    if not want and had:
        pot = [p for p in pot if p != PVD]
    result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"
    if want != had:
        result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | pvd: {why}")[:500]
    return why



# ---------------------------------------------------------------------------
# SPORTA ZĀLES VĀRTI (Raimonds 2026-10-27) — deterministiski, PĒC AI. Identiski abos repo.
#   0) aktīva Sporta zāle (Space_group) → paliek
#   1) tīra noliktava / ražošana / autoserviss → NOŅEM (klients tās pievieno filtrā pats)
#   2) pieejamība «not checked» → NOŅEM
#   3) plānojums Atvērts / Zāle+palīgtelpas  ∧  zāle ≥ 100 m²  ∧  zāle ≥ 60 % no platības
#      (labirints ar mazu zāli — Valdemāra 76 — NĒ) → PIELIEK, citādi NOŅEM
# ---------------------------------------------------------------------------
GYM = "Sporta zāle"


def apply_gym_gates(result, area_m2=None):
    pot = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
           if p.strip() and p.strip().lower() != "unknown"]
    had = GYM in pot
    sg = str(result.get("Space_group") or "")
    plan = str(result.get("Planojums") or "")
    area = _to_float(area_m2)
    hall = _to_float(result.get("Lielaka_zale_m2"))
    if hall is None and plan == "Atvērts":
        hall = area
    access = str(result.get("Vides_pieejamiba_check") or "unknown")

    if sg == GYM:
        want, why = True, "aktīva sporta zāle"
    elif sg in ("Noliktava", "Ražošana", "Autoserviss"):
        want, why = False, f"{sg} — izslēgts (klients pievieno filtrā)"
    elif access == "not checked":
        want, why = False, "nav pieejams"
    elif plan not in ("Atvērts", "Zāle + palīgtelpas"):
        want, why = False, f"plānojums {plan or 'unknown'}"
    elif hall is None or hall < 100:
        want, why = False, f"zāle {hall} < 100 m²"
    elif area and hall / area < 0.6:
        want, why = False, f"zāle tikai {round(100*hall/area)} % no platības (labirints)"
    else:
        want, why = True, f"zāle {hall:.0f} m² ({'?' if not area else round(100*hall/area)} %)"

    if want and not had:
        pot.append(GYM)
    if not want and had:
        pot = [p for p in pot if p != GYM]
    result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"
    if want != had:
        result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | sports: {why}")[:500]
    return why



# ---------------------------------------------------------------------------
# STOCKOFISS VĀRTI (Raimonds 2026-10-27) — PĒC AI. Identiski abos repo.
# Obligāti: paceļamie vārti (Pacelamie_varti_check=checked) ∧ ne augstāk par 1. stāvu
# ∧ ieeja no ielas nav «not checked». Citādi StockOfiss noņem gan no Potential, gan
# Space_group StockOfiss → Noliktava. Veikala-ieejas spriedumu dara modelis (prompts).
# ---------------------------------------------------------------------------
STOCK = "StockOfiss"


def apply_stockofiss_gates(result, floor_raw=None):
    pot = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
           if p.strip() and p.strip().lower() != "unknown"]
    gate = str(result.get("Pacelamie_varti_check") or "") == "checked"
    street = str(result.get("street_entrance") or "unknown")
    fl = _floor_number(floor_raw)
    ok = gate and street != "not checked" and (fl is None or fl <= 1)
    why = "ok" if ok else ("nav vārtu" if not gate else ("nav ieejas no ielas" if street == "not checked" else f"{fl}. stāvs"))
    if not ok:
        if STOCK in pot:
            pot = [p for p in pot if p != STOCK]
            result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | stockofiss: {why}")[:500]
        if str(result.get("Space_group") or "") == STOCK:
            result["Space_group"] = "Noliktava" if gate else "Tirdzniecība"
            result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | stockofiss→{result['Space_group']}: {why}")[:500]
    result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"
    return why



# ---------------------------------------------------------------------------
# BIROJS POTENCIĀLS NOLIKTAVĀM/RAŽOŠANAI/AUTOSERVISAM (Raimonds 2026-10-27).
# «Birojs potenciālā» = VISU telpu var pārtaisīt par biroju (nevis maza biroja telpa
# noliktavā — tā ir papildtelpa). Atļauts tikai: biroju / jaukta tipa ēka ∧ nav
# paceļamo vārtu ∧ nav pagrabs. Industriāla ēka / vārti → NOŅEM.
# ---------------------------------------------------------------------------
OFFICE = "Birojs"


def apply_office_from_industrial_gates(result, floor_raw=None):
    sg = str(result.get("Space_group") or "")
    # Veikals ar mega skatlogiem paliek veikals — Birojs potenciālu NOŅEM (Raimonds 2026-10-27)
    if sg == "Tirdzniecība" and str(result.get("Skatlogi") or "") == "Jā":
        _p = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
              if p.strip() and p.strip().lower() != "unknown"]
        if OFFICE in _p:
            _p = [p for p in _p if p != OFFICE]
            result["Potential_space_group"] = ", ".join(_p) if _p else "unknown"
            result["Debug_note"] = (str(result.get("Debug_note") or "") + " | birojs-pot: veikals ar skatlogiem")[:500]
        return "skatlogi"
    # Neapkurināts (Apkure=Nav) industriālā ēkā nevar būt birojs — ne aktīvs, ne potenciāls
    if str(result.get("Apkure") or "") == "Nav" and str(result.get("building_type") or "") == "Industriāla ēka":
        if sg == OFFICE:
            result["Space_group"] = sg = "Noliktava"
            result["Debug_note"] = (str(result.get("Debug_note") or "") + " | birojs→Noliktava: neapkurināts, industriāla ēka")[:500]
        _p = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
              if p.strip() and p.strip().lower() != "unknown" and p.strip() != OFFICE]
        result["Potential_space_group"] = ", ".join(_p) if _p else "unknown"
    if sg not in ("Noliktava", "Ražošana", "Autoserviss"):
        return "n/a"
    pot = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
           if p.strip() and p.strip().lower() != "unknown"]
    if OFFICE not in pot:
        return "nav"
    btype = str(result.get("building_type") or "")
    gate = str(result.get("Pacelamie_varti_check") or "") == "checked"
    fl = str(floor_raw or "").lower()
    basement = ("pagrab" in fl) or ("cokol" in fl) or (_floor_number(floor_raw) is not None and _floor_number(floor_raw) < 1)
    if btype in ("Biroju ēka", "Jaukta tipa ēka") and not gate and not basement:
        return "paliek"
    why = "industriāla ēka" if btype not in ("Biroju ēka", "Jaukta tipa ēka") else ("vārti" if gate else "pagrabs")
    pot = [p for p in pot if p != OFFICE]
    result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"
    result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | birojs-pot: {why}")[:500]
    return why



# ---------------------------------------------------------------------------
# ĒKAS KLASE A — CILVĒKA SARAKSTS (Raimonds 2026-10-27). A klasi nosaka cilvēks ēkas
# līmenī (a_klases_ekas.json, ķeksēts Word dokumentos). AI A → B, ja ēka NAV sarakstā;
# sarakstā esošai ēkai klase vienmēr A. Identiski abos repo.
# ---------------------------------------------------------------------------
_A_CLASS_IDS = None


def _load_a_class_ids():
    global _A_CLASS_IDS
    if _A_CLASS_IDS is None:
        import json as _json, os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "a_klases_ekas.json")
        try:
            with open(p, encoding="utf-8") as f:
                _A_CLASS_IDS = {int(b["building_profile_id"]) for b in _json.load(f)["buildings"]}
        except Exception:
            _A_CLASS_IDS = set()
    return _A_CLASS_IDS


def apply_building_class_gate(result, building_profile_id=None):
    ids = _load_a_class_ids()
    try:
        bid = int(building_profile_id) if building_profile_id else None
    except Exception:
        bid = None
    cls = str(result.get("building_class") or "")
    if bid is not None and bid in ids:
        if cls != "A":
            result["building_class"] = "A"
            result["Debug_note"] = (str(result.get("Debug_note") or "") + " | klase→A (apstiprināta ēka)")[:500]
        return "A (saraksts)"
    if cls == "A":
        result["building_class"] = "B"
        result["Debug_note"] = (str(result.get("Debug_note") or "") + " | klase A→B (ēka nav A sarakstā)")[:500]
        return "A→B"
    return cls or "unknown"



# ---------------------------------------------------------------------------
# IEŽOGOTA TERITORIJA — CILVĒKA SARAKSTS (Raimonds 2026-10-27). Nožogojumu bieži zina
# tikai aģents (bijis uz vietas). Prioritāte: iezogotas_ekas.json > bp.has_fenced > AI.
# ---------------------------------------------------------------------------
_FENCE_MAP = None


def _load_fence_map():
    global _FENCE_MAP
    if _FENCE_MAP is None:
        import json as _json, os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "iezogotas_ekas.json")
        try:
            with open(p, encoding="utf-8") as f:
                _FENCE_MAP = {int(b["building_profile_id"]): bool(b["fenced"]) for b in _json.load(f)["buildings"]}
        except Exception:
            _FENCE_MAP = {}
    return _FENCE_MAP


def apply_fence_gate(result, building_profile_id=None, building_flags=None):
    try:
        bid = int(building_profile_id) if building_profile_id else None
    except Exception:
        bid = None
    fm = _load_fence_map()
    val, src = None, None
    if bid is not None and bid in fm:
        val, src = fm[bid], "saraksts"
    elif (building_flags or {}).get("has_fenced") is not None:
        val, src = bool(building_flags["has_fenced"]), "ēkas profils"
    if val is None:
        return str(result.get("Nozogota_teritorija_check") or "unknown")
    new = "checked" if val else "not checked"
    if str(result.get("Nozogota_teritorija_check") or "") != new:
        result["Nozogota_teritorija_check"] = new
        result["Debug_note"] = (str(result.get("Debug_note") or "") + f" | žogs={new} ({src})")[:500]
    return f"{new} ({src})"



# ---------------------------------------------------------------------------
# IEVAS ZINĀŠANAS ĒKAS LĪMENĪ (2026-10-28, ievas_ekas.json). Cilvēks > ēkas poga > AI.
#   phase="facts" (PIRMS med vārtiem): pieejamība, apsardze, konferenču zāle
#   phase="med"   (PĒC med vārtiem):   Med kods var → Medicīna; nevar → noņem
# ---------------------------------------------------------------------------
_IEVA = None


def _load_ieva():
    global _IEVA
    if _IEVA is None:
        import json as _json, os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "ievas_ekas.json")
        try:
            with open(p, encoding="utf-8") as f:
                _IEVA = {int(b["building_profile_id"]): b for b in _json.load(f)["buildings"]}
        except Exception:
            _IEVA = {}
    return _IEVA


def apply_ieva_gates(result, building_profile_id=None, phase="facts"):
    try:
        bid = int(building_profile_id) if building_profile_id else None
    except Exception:
        bid = None
    rec = _load_ieva().get(bid) if bid is not None else None
    if not rec:
        return "nav Ievas datu"
    done = []

    def _set(field, val, label):
        if str(result.get(field) or "") != val:
            result[field] = val
            done.append(f"{label}={val}")

    if phase == "facts":
        if "pieejama" in rec:
            _set("Vides_pieejamiba_check", "checked" if rec["pieejama"] else "not checked", "pieejama")
            result["Vides_pieejamiba_iemesls"] = ("Ievas zināšanas: ratiņkrēslā var iekļūt" if rec["pieejama"]
                                                  else "Ievas zināšanas: ratiņkrēslā nevar iekļūt")
        if "apsardze" in rec:
            _set("Apsargajama_teritorija_check", "checked" if rec["apsardze"] else "not checked", "apsardze")
        if "konf_zale" in rec:
            _set("Has_conference_room", "checked" if rec["konf_zale"] else "not checked", "konf")
    elif phase == "med" and "med" in rec:
        pot = [p.strip() for p in str(result.get("Potential_space_group") or "").split(",")
               if p.strip() and p.strip().lower() != "unknown"]
        if rec["med"] and "Medicīna" not in pot and str(result.get("Space_group") or "") != "Medicīna":
            pot.append("Medicīna"); done.append("med+")
        if not rec["med"] and "Medicīna" in pot:
            pot = [p for p in pot if p != "Medicīna"]; done.append("med-")
        result["Potential_space_group"] = ", ".join(pot) if pot else "unknown"
    if done:
        result["Debug_note"] = (str(result.get("Debug_note") or "") + " | Ieva: " + ", ".join(done))[:500]
    return ", ".join(done) or "bez izmaiņām"


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
    if "[pv:" not in str(data.get("Debug_note", "")):
        data["Debug_note"] = f"{str(data['Debug_note'])[:440]} [pv:{PROMPT_VERSION}]"

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