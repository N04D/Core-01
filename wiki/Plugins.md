# Plugins

Alle plugins registreren zichzelf idempotent in `plugin_registry` en volgen het gedeelde CLI-contract `--register`, `--event_id` en `--db`.

## Invoer

### Telegram Inbound Hub

Bestand: `plugins/inputs/telegram_in.py`

Verwerkt tekst, foto's en video's via de Telegram Bot API. Configureer `TELEGRAM_BOT_TOKEN` in `.env`; beperk productie-invoer optioneel met `TELEGRAM_ALLOWED_CHAT_IDS`. De offline testmodus gebruikt `--mock-message`. Media wordt begrensd door `TELEGRAM_MAX_MEDIA_BYTES` (standaard 100 MiB).

## Media

### NightCafe Local Media

Bestand: `plugins/media/media_nightcafe.py`

Indexeert afbeeldingen en video's recursief uit een lokale NightCafe-map. Prompts en modelmetadata worden gelezen uit gelijknamige `.json`- of `.txt`-sidecars en, wanneer Pillow beschikbaar is, uit ingebedde afbeeldingsmetadata.

```bash
./venv/bin/python3 plugins/media/media_nightcafe.py \
  --register --index --db db/events.db --source-dir /pad/naar/NightCafe
```

De standaardbron kan via `NIGHTCAFE_MEDIA_DIR` worden ingesteld. Nieuwe providers, zoals Google Drive, implementeren hetzelfde `MediaProvider`-contract.

## AI

### Lokale RTX 3090 Generator

Bestand: `plugins/ai/gen_local_llm.py`

Rendert een skill via `core/markdown_parser.py`, stuurt de prompt via `stdin=subprocess.PIPE` naar een lokale modelcommandoregel en bewaart Markdown in `vault/concepten/`.

- Productie: `LOCAL_LLM_COMMAND="ollama run llama3.1:8b"`
- Test: `LOCAL_LLM_MOCK=1`
- Eventtype: `AI_GENERATION`

## I/O

### Firecrawl Web Scraper

Bestand: `plugins/io/crawl_firecrawl.py`

Leest een URL uit de eventpayload, vraagt Markdown op via de lokale Firecrawl REST-API en schrijft een uniek document met bronfrontmatter naar `vault/research/`. De verrijkte payload bevat `filepath`.

- Standaard-API: `http://localhost:3002`
- Variabelen: `FIRECRAWL_API_URL`, `FIRECRAWL_API_KEY`, `FIRECRAWL_TIMEOUT_SECONDS`
- Eventtype: `CRAWL_URL`

## Kanalen

### Mock Publisher

Bestand: `plugins/channels/pub_mock.py`

Veilige zandbakpublisher. Leest `content` of `draft_file` en schrijft de onderschepte publicatie naar `vault/logs/mock_publish_log.md`.

- Pluginnaam: `Mock Publisher (Test Zandbak)`
- Eventtype: `PUBLISH_MOCK`

### LinkedIn Publisher

Bestand: `plugins/channels/pub_linkedin.py`

Playwright-publisher voor profielposts. Ondersteunt headed/headless uitvoering, veilige dry-run, storage-state en fout-screenshots. De test-only authfallback bouwt uitsluitend een lokale composer en maakt geen LinkedIn-verbinding.

- Pluginnaam: `LinkedIn Publisher (Productie)`
- Eventtype: `PUBLISH_LINKEDIN`
- Auth: `config/linkedin_auth.json`, mode `0600`

Deze legacy-plugin blijft beschikbaar voor compatibiliteit maar hoort in productie
uitgeschakeld te zijn wanneer de Pro-plugin actief is.

### LinkedIn Pro Publisher & Analytics

Bestand: `plugins/channels/pub_linkedin_pro.py`

Uitgebreide adapter voor:

- Profiel- en bedrijfspaginapublicaties (`--company-id`).
- Nieuwsbriefartikelen (`--newsletter-id`).
- Deterministische teasers bij artikelen en links.
- Recente-postanalytics met likes, views en comments (`--analytics`).
- Profiel-/bedrijfscontext met headline, bio en ervaring (`--fetch-bio`).
- JSON- en Markdownartifacts in `vault/analytics/` en `vault/research/`.
- Multimodale profiel-, bedrijfspagina- en nieuwsbriefcontent via `--text`,
  `--image-path` (JPG/PNG) en `--video-path` (MP4/MOV/M4V/WebM).
- Playwright file-inputuploads voor afbeeldingen en video, met een gevalideerd
  media-manifest in dry-run- en mockresultaten.

Zonder auth-state wordt `AUTH_REQUIRED` als expliciet mockresultaat opgeslagen en wordt LinkedIn niet benaderd.

### Substack Pro Publisher & Analytics

Bestand: `plugins/channels/pub_substack_pro.py`

Volwaardige multimodale Substack-adapter voor artikelen, Notes en operationele
feedback:

- `--post-article`: titel, optionele `--subheader`, body en media.
- `--post-note`: korte tekst met optionele afbeelding en/of video.
- `--analytics`: abonneegroei, views en engagement.
- `--read-comments`: auteurs en reactietekst van artikelen/Notes.
- `--fetch-profile`: profiel- en publicatiecontext voor AI-playbooks.
- `--text`, `--image-path` en `--video-path` zijn zowel CLI- als
  event-payloadvelden.

Analytics en comments worden als JSON én Markdown opgeslagen in
`vault/analytics/`; profieldata komt in `vault/research/`. Browserfouten leveren
een screenshot in `vault/logs/screenshots/`. Zonder
`config/substack_auth.json` ontstaat een veilig `AUTH_REQUIRED`-mockresultaat
met `substack_contacted: false`; ontbrekende media worden dan gerapporteerd in
plaats van geüpload.

### Medium Publisher

Bestand: `plugins/channels/pub_medium_pro.py`

Playwright-kanaal voor Medium-verhalen met:

- Inline `--text` of Markdown via `draft_file`/`filepath`.
- Automatische titel uit `title`, `topic` of de eerste Markdown-heading.
- Een optionele JPG/PNG-header via `--image-path`.
- Maximaal vijf genormaliseerde publicatietags via `--tags`.
- `--dry-run` en configureerbare `--headless` browsermodus.
- Automatische route `PUBLISH_MEDIUM` tijdens `--register`.
- Fout-screenshots in `vault/logs/screenshots/`.

Zonder `config/medium_auth.json` wordt een veilig `AUTH_REQUIRED`-resultaat in
de eventpayload geschreven met `medium_contacted: false` en status
`COMPLETED`; Medium wordt dan niet benaderd.

## Registreren en routeren

```bash
./venv/bin/python3 plugins/channels/pub_mock.py --register --db db/events.db
./venv/bin/python3 plugins/ai/gen_local_llm.py --register --db db/events.db
./venv/bin/python3 plugins/io/crawl_firecrawl.py --register --db db/events.db
./venv/bin/python3 plugins/channels/pub_linkedin_pro.py --register --db db/events.db
./venv/bin/python3 plugins/channels/pub_substack_pro.py --register --db db/events.db
./venv/bin/python3 plugins/channels/pub_medium_pro.py --register --db db/events.db
```

Routes kunnen idempotent via SQLite worden beheerd. Controleer ze met `scripts/system_status.py` voordat een productieflow start.

De oudere **Substack Publisher**-registratie is legacy; activeer slechts één
Substack-route tegelijk om dubbele dashboardkaarten te voorkomen. De actieve
Pro-route gebruikt `config/substack_auth.json` en kan via de gedeelde Chrome-CDP
sessie worden gekoppeld.
