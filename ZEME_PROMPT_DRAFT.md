# ZEMES AI — ŠABLONS (Raimonds rediģē, tad Melnā iestrādā kodā)

## Būtība (kā Raimonds grib)
- **Robots ar fiksētu ŠABLONU**, ne brīvs teksts. Katrs zemes apraksts iznāk vienā un tajā pašā struktūrā.
- Info ņem **GAN no teksta, GAN no bildēm**. NEbalstās tikai uz tekstu — daudzam sludinājumam teksts būs tikai "Pārdod šo zemes gabalu", tāpēc svarīgo (komunikācijas, ēka uz gabala, pievadceļš, reljefs, zonas plāns) meklē arī BILDĒS.
- Katra lieta = **atsevišķs DB lauks**, ko AI aizpilda **tikai ja atrod** (citādi paliek tukšs / NULL). JSON ar enum, kur der.
- **Backup**: ja kāda info nav savācama, šablona teikums to vienkārši izlaiž — apraksts joprojām izskatās normāli.
- Pielietojums (`land_use`) nāk no ss.lv strukturētā lauka (`tdo_228`) — AI to NEĢENERĒ.
- Lokācija (rajons/pilsēta/iela) + platība + cena nāk no skrāpera — AI tos NEĢENERĒ, tikai lieto šablonā.

---

## ŠABLONS (fiksēta struktūra — AI aizpilda tukšās vietas, izlaiž to, kā nav)

**Bāze (vienmēr) — BEZ adreses (tā ir titulā!):**
> Tiek {pārdots|iznomāts} zemes gabals {platība} m². Pielietojums — {pielietojums}.

**+ Komunikācijas (ja atrastas tekstā vai bildēs):**
> Zemes gabalam pieejamas {visas komunikācijas (elektrība, ūdens, kanalizācija, gāze) | uzskaitījums, piem. "elektrība un ūdens"}.

**+ Zonējums (ja atrasts) — fiksētais bloks no zonējuma tabulas zemāk:**
> Zonējums zemes gabalam ir {kods} - kas ir {nosaukums}. {izmantojums}.

**+ Ēka uz gabala (ja ir):**
> Uz gabala ir ēka (~{ēkas platība} m²).

**+ Papildus (ja ir — saskaņots projekts / pievadceļš / servitūts / agenta komentārs):**
> {papildus_info}.

Ja komunikācijas/zona/papildus nav → tos teikumus vienkārši NErakstām.

---

## PROMPTS (melnraksts — AI tam vienmēr jāseko kā robotam)

```
# SS.LV ZEMES (PLOT) SLUDINĀJUMA ANALĪZE — ŠABLONA REŽĪMS

## GOAL
Analyze one SS.lv LAND (zeme) listing and fill a FIXED set of structured
fields, using BOTH the listing text AND all images. Then assemble a
description strictly from the template. You are a robot: always the same
structure, never free prose.

## SOURCES (equal weight — DO NOT rely on text alone)
Many ads have almost no text (e.g. "Pārdod šo zemes gabalu"). You MUST inspect
every image to recover facts:
- Power lines / poles on or next to the plot -> elektrība likely available
- Visible water/gas pipes, hydrants, manholes -> ūdens/kanalizācija/gāze
- A building/structure standing on the plot -> note it in papildus
- Visible road / driveway (asphalt/gravel) -> access in papildus
- A plan / cadastral map / zoning sketch image -> zona, apbūves parametri
- Terrain (forest, water edge, cleared/prepared ground)

## HARD RULES
0. READ THE ENTIRE listing text to the end. The zoning code (JC8, DzD1, R, ...)
   and utilities are OFTEN stated lower in the text, not at the top. Never decide
   from the first paragraph alone.
1. Do NOT invent. A field is filled ONLY if the text or an image supports it;
   otherwise return "Nav minēts" (that field stays empty in DB).
2. Use ONLY the allowed enum values. Output must match the JSON schema exactly.
3. No markdown, no commentary outside JSON.
4. Do NOT output the land use (pielietojums), location, area or price — those
   come from structured data, not you.
5. Never output commercial-premises fields (ceiling, heating, WC, floor).

## FIELDS

### communications  (enum, multi-select; text OR image)
Any subset of: ["elektrība","ūdens","kanalizācija","gāze","siltums"].
- If ALL of elektrība+ūdens+kanalizācija+gāze are present -> also fine to
  return all four (UI shows "visas komunikācijas").
- If nothing about utilities in text or images -> [] (empty).
Evidence from images counts (power lines = elektrība, etc.).

### zoning  (string; text OR plan/map image)
Return the functional-zone CODE if identifiable (from text or a plan/map image):
one of DzS, DzS1, DzM, DzM1, DzD, DzD1, JC, JC2, JC4, P, R, TR, TA, DA, M, L, Ū.
Return the exact code as written (e.g. "JC2", "DzD1"). If a zone is described
in words but no code is given, return the closest code. Else "Nav minēts".
(The human-readable sentence is added by OUR code from the zoning table — do NOT
write the explanation yourself.)

### building_on_plot  (enum: ["nav","ir_eka","ir_pamati","nezināms"])
Is there a structure already on the plot (from text or a photo)? Else "nezināms".

### building_area_m2  (string; text OR image)
If there IS a building on the plot AND its floor area is stated in text
("ēkas platība 411 m2") or clearly derivable -> return digits only (m²).
Else "Nav minēts". (Only for land with an existing building.)

### access  (string; text OR image)
Access/road: "asfaltēts piebraucamais ceļš", "grants ceļš", "ceļa servitūts",
"ērta izbrauktuve uz ...". Else "Nav minēts".

### extra_info  (string)
One short clause of the single most valuable EXTRA fact, if any:
saskaņots būvprojekts, karjers/resursi, sagatavots būvniecībai, esoša ēka
pārbūvei, u.tml. Else "Nav minēts". No price, no phone, no agent name.

### listing_intent  (enum: ["pārdod","iznomā","pērk_meklē","nezināms"])
"Pērku", "Vēlos iegādāties", "Vēlas nomāt", "Куплю" -> "pērk_meklē".

### land_description  (string — ASSEMBLED FROM THE TEMPLATE)
Fill the fixed template below, OMITTING any clause whose field is empty.
Deal word from price type (pārdod/iznomā). Location, area, land use are given
to you as inputs (do not change them).

TEMPLATE (each [ ... ] clause is dropped when its field is empty):
"Tiek {pārdots|iznomāts} zemes gabals {platība} m². Pielietojums — {pielietojums}.
[ Zemes gabalam pieejamas {komunikācijas}.][ {ZONING BLOCK from our table by code}.]
[ Uz gabala ir ēka (~{building_area_m2} m²).][ {extra_info}.]"

RULES:
- NO address / street / city / district — it is already in the listing TITLE. Never repeat it.
- Write in OUR clean, neutral style. Do NOT copy the seller's sentences or marketing.
- The zoning sentence is NOT written by you — our code inserts the fixed block for
  the code you returned in `zoning`.
- extra_info = only concrete facts (two plots, cleared/fenced, existing building,
  approved project, servitude). No "near IKEA / great location / call us" marketing.

### Confidence  (string "0.00"–"1.00")

### Debug_status  (enum: ["ok","wanted_ad","low_evidence","agent_detected"])
"wanted_ad" whenever listing_intent = "pērk_meklē".

## OUTPUT
Return ONLY the JSON object. No markdown.
```

---

## JSON SHĒMA (lauki → DB kolonnas)

```
communications    : string[]  (enum: elektrība|ūdens|kanalizācija|gāze|siltums)
zoning            : string
building_on_plot  : enum ["nav","ir_eka","ir_pamati","nezināms"]
building_area_m2  : string   (ēkas platība m², ja uz gabala ir ēka)
access            : string
extra_info        : string
listing_intent    : enum ["pārdod","iznomā","pērk_meklē","nezināms"]
land_description  : string   (šablona rezultāts)
Confidence        : string
Debug_status      : enum ["ok","wanted_ad","low_evidence","agent_detected"]
```

Tukšs = "Nav minēts" (string) vai [] (masīvs) → DB paliek NULL/tukšs.
NAV šeit (ar nolūku): land_use (no ss.lv lauka), lokācija/platība/cena (no skrāpera),
komerc-lauki (griesti, apkure, WC, grīda).

---

## ZONĒJUMA ŠABLONI (kods → fiksēts teikums, ko kods ieliek aprakstā)

AI atgriež tikai KODU (`zoning`). Mūsu kods pēc koda ieliek šo teikumu. Sakritība:
vispirms precīzais kods (JC4), citādi bāzes burti (JC2→JC, DzS1→DzS, DzD1→DzD).

Katram kodam SAVS pilns, izskaidrojošs teksts (copy-paste, kā WP eksporta šablonos).

- **DzS / DzS1** — Zemes gabalam ir savrupmāju dzīvojamās apbūves zonējums ar kodu (DzS), kas nozīmē, ka tas ir paredzēts privātmāju un individuālo dzīvojamo ēku būvniecībai. Komercdarbība šajā zonā ir ļoti ierobežota.
- **DzM / DzM1** — Zemes gabalam ir mazstāvu dzīvojamās apbūves zonējums ar kodu (DzM), kas nozīmē, ka tajā var būvēt rindu mājas un nelielas mazstāvu daudzdzīvokļu ēkas.
- **DzD / DzD1** — Zemes gabalam ir daudzdzīvokļu dzīvojamās apbūves zonējums ar kodu (DzD), kas nozīmē, ka tajā var būvēt daudzdzīvokļu mājas, un ēku pirmajos stāvos var izvietot arī veikalus, birojus un pakalpojumus.
- **JC** (un citi JCx bez atsevišķa teksta) — Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, kā arī noliktavu vai vieglās ražošanas funkciju attīstībai.
- **JC2** — Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, kā arī noliktavu, vairumtirdzniecības vai vieglās ražošanas funkciju attīstībai.
- **JC4** — Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, noliktavu, vairumtirdzniecības un vieglās ražošanas funkciju attīstībai.
- **JC8** — Zemes gabalam ir jauktas centra apbūves zonējums ar kodu (JC8) — funkcionālā zona Rīgas vēsturiskā centra un tā aizsardzības zonas teritorijā ar plašu jauktas izmantošanas spektru: dzīvojamā apbūve (savrupmājas, rindu un daudzdzīvokļu mājas), biroji, tirdzniecība un pakalpojumi, tūrisma un atpūtas, kultūras, sporta, izglītības, veselības un sociālās aprūpes iestādes; papildus atļauta arī vieglā rūpniecība un transporta infrastruktūra.
- **P** — Zemes gabalam ir publiskās apbūves zonējums ar kodu (P), kas nozīmē, ka tas ir paredzēts birojiem, izglītības, medicīnas, sporta un pakalpojumu objektiem.
- **R** — Zemes gabalam ir ražošanas (industriālās apbūves) zonējums ar kodu (R), kas nozīmē, ka tas ir paredzēts noliktavām, ražošanas uzņēmumiem un loģistikas objektiem.
- **TR** — Zemes gabalam ir transporta infrastruktūras zonējums ar kodu (TR), kas nozīmē, ka tas ir paredzēts ceļiem, transporta koridoriem, stāvvietām un transporta apkalpes objektiem.
- **TA** — Zemes gabalam ir tehniskās infrastruktūras zonējums ar kodu (TA), kas nozīmē, ka tas ir paredzēts inženiertīkliem — elektroapgādei, ūdensapgādei, kanalizācijai, sakariem un atkritumu apsaimniekošanai.
- **DA** — Zemes gabalam ir dabas un apstādījumu teritorijas zonējums ar kodu (DA), kas nozīmē, ka tas ir parks vai zaļā zona, un apbūves potenciāls tajā parasti ir zems.
- **M** — Zemes gabalam ir meža teritorijas zonējums ar kodu (M), kas nozīmē, ka apbūve tajā ir ļoti specifiska un stingri ierobežota.
- **L** — Zemes gabalam ir lauksaimniecības teritorijas zonējums ar kodu (L), kas nozīmē, ka tas ir paredzēts lauksaimnieciskajai ražošanai un viensētām, reizēm arī tūrisma vai servisa objektiem.
- **Ū** — Zemes gabalam ir ūdens teritorijas zonējums ar kodu (Ū), kas nozīmē, ka tas ir paredzēts piestātnēm, ūdens izmantošanai un specifiskiem ar ūdeni saistītiem objektiem.

Sakritība: precīzais kods vispirms (JC2, JC4, JC8). Citi JCx bez sava teksta → JC.
Pārējiem bāzes burti (DzS1→DzS, DzM1→DzM, DzD1→DzD).
Šis teksts aizstāj šablonā `[ Zonējuma bloks. ]`. Ja `zoning`="Nav minēts" → izlaiž.

## ŠABLONS DZĪVĒ — uz mūsu 3 reālajiem gabaliem

**Dambja 6b, Sarkandaugava, 5844 m², Komerciāla apbūve, zona R** (bagāts teksts):
> Tiek pārdots zemes gabals 5844 m². Pielietojums — komerciāla apbūve. Zemes gabalam pieejamas visas komunikācijas (elektrība, ūdens, kanalizācija, gāze). Zemes gabalam ir ražošanas (industriālās apbūves) zonējums ar kodu (R), kas nozīmē, ka tas ir paredzēts noliktavām, ražošanas uzņēmumiem un loģistikas objektiem. Nodibināts ceļa servitūts piekļuvei.

**Ilūkstes 58b, Purvciems, 1578 m², Daudzstāvu būve, zona DzD:**
> Tiek pārdots zemes gabals 1578 m². Pielietojums — daudzstāvu būve. Zemes gabalam ir daudzdzīvokļu dzīvojamās apbūves zonējums ar kodu (DzD), kas nozīmē, ka tajā var būvēt daudzdzīvokļu mājas, un ēku pirmajos stāvos var izvietot arī veikalus, birojus un pakalpojumus. Ir saskaņots kluba tipa daudzdzīvokļu mājas būvprojekts uz 24 dzīvokļiem.

**Slokas 24a, Āgenskalns, 1580 m² — BACKUP (teksts = "Tiek pārdots zemes gabals. Īpašnieks"):**
> Tiek pārdots zemes gabals 1580 m². Pielietojums — daudzstāvu būve.
> *(komunikācijas/zona/papildus nav → izlaisti; apraksts joprojām korekts)*

---

## KO VĒL VAJAG (pēc šablona apstiprināšanas — Melnā izdara)
1. **Migrācija**: jauni listings/scrape_inbox lauki — `communications text[]`, `zoning`, `building_on_plot`, `building_area_m2`, `access`, `extra_info`, `listing_intent`, `land_description`.
2. **AI routing**: zemi (group='zeme') sūtīt caur ŠO promptu, ne komerc-promptu. → inbox→listings zemei vairs NEuzstāda uzreiz Debug_status='ok'; atstāj tukšu, lai AI runner to apstrādā.
3. **WP eksports**: zemes apraksts = `land_description` (šablons), ne komerc-teksts.
4. **wanted_ad** (pērk/meklē) → nepublicē, atstāj manuālai pārskatei.
5. **Backfill** esošajiem zemes listingiem (tiem Space_group='unknown', bez šiem laukiem).
