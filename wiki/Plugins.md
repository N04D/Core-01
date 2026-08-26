# Plugins

Alle plugins registreren zichzelf idempotent in `plugin_registry` en volgen het gedeelde CLI-contract `--register`, `--event_id` en `--db`.

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

### LinkedIn Pro Publisher & Analytics

Bestand: `plugins/channels/pub_linkedin_pro.py`

Uitgebreide adapter voor:

- Profiel- en bedrijfspaginapublicaties (`--company-id`).
- Nieuwsbriefartikelen (`--newsletter-id`).
- Deterministische teasers bij artikelen en links.
- Recente-postanalytics met likes, views en comments (`--analytics`).
- Profiel-/bedrijfscontext met headline, bio en ervaring (`--fetch-bio`).
- JSON- en Markdownartifacts in `vault/analytics/` en `vault/research/`.

Zonder auth-state wordt `AUTH_REQUIRED` als expliciet mockresultaat opgeslagen en wordt LinkedIn niet benaderd.

## Registreren en routeren

```bash
./venv/bin/python3 plugins/channels/pub_mock.py --register --db db/events.db
./venv/bin/python3 plugins/ai/gen_local_llm.py --register --db db/events.db
./venv/bin/python3 plugins/io/crawl_firecrawl.py --register --db db/events.db
./venv/bin/python3 plugins/channels/pub_linkedin_pro.py --register --db db/events.db
```

Routes kunnen idempotent via SQLite worden beheerd. Controleer ze met `scripts/system_status.py` voordat een productieflow start.
