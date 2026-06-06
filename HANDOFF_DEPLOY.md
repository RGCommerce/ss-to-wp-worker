# HANDOFF → Zilā kaste: ss-to-wp-worker deploy (Melnā kaste, 2026-06-07)

## ⛔ VIENS SOLIS: merge `melna` → `main` + Railway redeploy

Repo `RGCommerce/ss-to-wp-worker`. Zars `melna` ir **priekšā main par 2 commitiem** (tīrs, kompilējas):

```
git checkout main && git pull && git merge melna && git push origin main
```

Commiti (`origin/main..origin/melna`):
- `efcd985` — AI bilžu enhance: OpenAI kļūdas iemesls līdz UI (`EnhanceError`) + skaidrs HTTP statuss/ziņa.
- `092f75f` — **AI enhance Replicate (Seedream) dzinējs** kā alternatīva OpenAI; `/agent/image-enhance` jauns `engine` param (`openai`|`replicate`).

## Kāpēc tas vajadzīgs TAGAD

Panelis (`melna`→`main`) JAU iemerdžots + dzīvs — anketā ir **2 AI pogas** (✨ OpenAI / 🪄 Replicate). BET worker `main` vēl **NEzina `engine` param** → spiežot 🪄 Replicate, vecais worker lauku ignorē un palaiž **OpenAI**. Tāpēc Replicate dashboard tukšs (0 maksas) un bildes nāk no OpenAI (mēdz halucinēt).

Pēc šī merge+redeploy: 🪄 Replicate reāli aizies uz Seedream (tas pats dzinējs kā DB→WP `image_pipeline`).

## Pārbaude pēc deploya

1. Anketā uz vienas bildes spied 🪄 Replicate → Replicate dashboardā parādās jauns prediction (maksa ~$0.04).
2. Otrai bildei ✨ OpenAI → salīdzini. Faili paliek blakus: `_enhanced.png` (openai) vs `_enhanced_repl.jpg` (replicate).
3. Ja AI kļūda — uzbrauc ar peli sarkanajam "Kļūda" → tagad rāda īsto iemeslu (`EnhanceError`).

ENV: `REPLICATE_API_TOKEN` Railway worker jau ir (lieto `image_pipeline`).
