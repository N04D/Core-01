# Event-Driven AI OS

## Visie

Dit project bouwt een soeverein, lokaal AI-besturingssysteem voor Linux op een Raspberry Pi 5 (ARM64). Automatisering, onderzoeksdata, prompts, concepten en publicaties blijven lokaal beheersbaar. Externe diensten zijn adapters rond een lokale kern en kunnen worden vervangen zonder de workflowarchitectuur te wijzigen.

## Wat er staat

- Een duurzame SQLite-eventbus met atomische taakclaims, retries en een dead-letter queue.
- Een continue worker-daemon die events aan actieve plugins routeert.
- Een folder-watchdog die Markdown uit een Obsidian-vault omzet in events.
- YAML-frontmattertemplates met fail-fast inputvalidatie en veilige placeholders.
- Lokale AI-generatie via Ollama/een configureerbaar subprocess en een testmock.
- Firecrawl-research, mock-publicatie en LinkedIn-kanalen.
- Master-playbooks voor research → generatie → publicatie.
- Read-only statusdashboard, integratietests en systemd-units.

## Huidige status

De lokale mockketen is end-to-end getest: een bestand in `vault/uitgaand/` wordt gedetecteerd, gearchiveerd, als event geclaimd en gepubliceerd via het mockkanaal. LinkedIn ondersteunt veilige dry-runs en een auth-loze mockfallback. Live LinkedIn-gebruik vereist een geldige, lokaal opgeslagen Playwright storage-state.

## Belangrijke locaties

| Pad | Functie |
|---|---|
| `core/` | Database-initialisatie en Markdownparser |
| `daemon/` | Eventworker en folder-watchdog |
| `plugins/` | AI-, I/O- en publicatieadapters |
| `playbooks/` | Orchestrators voor samengestelde workflows |
| `scripts/` | Self-tests en statusdashboard |
| `vault/` | Obsidian-data, skills, research, concepten en logs |
| `deploy/` | systemd-units en installatieprogramma |

## Snelle start

```bash
./venv/bin/python3 core/setup_database.py --database db/events.db
LOCAL_LLM_MOCK=1 ./venv/bin/python3 daemon/worker.py --database db/events.db
./venv/bin/python3 playbooks/master_workflow.py \
  --db db/events.db \
  --topic "Soevereine lokale AI"
```

Bekijk de actuele toestand met:

```bash
./venv/bin/python3 scripts/system_status.py --db db/events.db
```
