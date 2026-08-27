# Playbooks en tests

## RAG-verrijkte generatie

Alle playbooks die `AI_GENERATION` dispatchen gebruiken automatisch de lokale vaultindex via `plugins/ai/gen_local_llm.py`. `RAG_AUTO_INDEX=0` schakelt de incrementele refresh uit; `RAG_CONTEXT_CHUNKS` bepaalt het maximale aantal contextchunks (standaard vijf).

```bash
./venv/bin/python3 core/rag_index.py --db db/events.db --query "lokale AI"
./venv/bin/python3 scripts/test_rag_editorial.py
```

## Master Workflow

`playbooks/master_workflow.py` bestuurt een opeenvolgende baton-estafette:

1. Optioneel `CRAWL_URL` wanneer `--url` aanwezig is.
2. `AI_GENERATION` met researchcontext en een gevalideerd skill-template.
3. `PUBLISH_MOCK` of `PUBLISH_LINKEDIN` met het gegenereerde concept.

Elke dispatch valideert eerst de actieve route en executable. De orchestrator pollt live op status, detecteert dead letters en rapporteert event-ID, eventtype, retryteller en foutreden.

```bash
./venv/bin/python3 playbooks/master_workflow.py \
  --db db/events.db \
  --topic "Soevereine Lokale AI Architecturen" \
  --publish-channel PUBLISH_MOCK
```

Met `--live` wordt alleen automatisch `PUBLISH_LINKEDIN` gekozen wanneer `config/linkedin_auth.json` bestaat. Zonder die auth-state volgt een gelogde fallback naar `PUBLISH_MOCK`.

```bash
./venv/bin/python3 playbooks/master_workflow.py \
  --db db/events.db \
  --topic "Lokale AI" \
  --live
```

`playbooks/pb_runner.py` is de compacte eerdere research/generation/publication-runner en blijft bruikbaar voor eenvoudige lokale flows.

## Self-tests

| Script | Bereik |
|---|---|
| `scripts/test_pipeline.py` | Database, parser, mock-LLM en conceptoutput |
| `scripts/test_daemon_loop.py` | Atomisch claimen, routeren en voltooien |
| `scripts/test_live_obsidian_file.py` | Echt vaultbestand → watchdog → event → worker → archief |
| `scripts/test_linkedin_dryrun.py` | Veilige visuele LinkedIn-composer zonder publishklik |
| `scripts/test_linkedin_pro_mock.py` | Auth-loze bio- en analyticsartifacts |

Voorbeelden:

```bash
./venv/bin/python3 scripts/test_pipeline.py
./venv/bin/python3 scripts/test_daemon_loop.py
./venv/bin/python3 scripts/test_live_obsidian_file.py --db db/events.db
./venv/bin/python3 scripts/test_linkedin_pro_mock.py
```

## Operationele observatie

```bash
./venv/bin/python3 scripts/system_status.py --db db/events.db --limit 20
```

Het dashboard toont integriteit, plugins, routes, recente events, retries en dead letters zonder de database te wijzigen.

## Master Syndication Suite

`playbooks/master_syndication_suite.py` voert de volledige productie-/testketen
zelfstandig uit:

1. Een diepgaand centraal essay via `AI_GENERATION`.
2. Fan-out naar alle gevraagde, actieve routes (standaard LinkedIn Pro,
   Substack Pro en Medium).
3. Bounded workercycli tot alle events terminaal zijn.
4. Analytics, comments en profielcontext voor plugins die deze capabilities
   aanbieden.
5. Een terminalrapport met eventstatussen, kanaalarchieven en insightartifacts.

Veilige volledige mocktest:

```bash
./venv/bin/python3 playbooks/master_syndication_suite.py \
  --topic "Soevereine AI Syndicatie" \
  --db db/events.db \
  --mock
```

Gebruik `--channels` om eventtypes te selecteren, `--image-path` voor een
gedeelde JPG/PNG-asset en `--no-collect-insights` om de analysefase over te
slaan.
