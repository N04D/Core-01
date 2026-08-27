# Architectuur

## Eventgedreven kern

SQLite vormt de duurzame eventbus. Producenten schrijven events naar `events_queue`; de worker claimt atomisch het oudste `PENDING` event via `UPDATE ... RETURNING` en zet dit op `PROCESSING`. Hierdoor kunnen meerdere workers concurreren zonder dezelfde taak normaal dubbel te claimen.

De kernstatussen zijn:

```text
PENDING → PROCESSING → COMPLETED
                  └─→ retry → PENDING
                  └─→ na 3 fouten → dead_letter_queue
```

`event_routes` koppelt een `event_type` aan een pluginnaam. `plugin_registry` bevat het actieve executable-pad, type en icoon. De worker start plugins zonder shell en geeft `--event_id` en `--db` door.

## Databaseobjecten

- `events_queue`: actieve en voltooide events, JSON-payload, status, retries en foutlog.
- `dead_letter_queue`: definitief mislukte events inclusief doodsoorzaak.
- `event_routes`: stabiele eventtype-naar-pluginroutering.
- `plugin_registry`: uitvoerbare plugininventaris met activatiestatus.
- `scheduled_events`: toekomstige events met UTC-uitvoertijd en dispatchstatus.
- `media_sources`: geconfigureerde lokale of externe mediaproviders.
- `media_assets`: genormaliseerde, doorzoekbare assetmetadata en beschikbaarheid.
- `media_links`: koppelingen van assets aan drafts en geplande events.

Initialiseren:

```bash
./venv/bin/python3 core/setup_database.py --database db/events.db
```

## Publicatie-agenda

`daemon/scheduler.py` claimt verlopen planningen met `BEGIN IMMEDIATE` en schrijft de planning en het nieuwe queue-event in één transactie weg. Hierdoor kan een planning niet dubbel worden gepubliceerd door concurrerende schedulers.

## Inkomende Telegram-hub

`plugins/inputs/telegram_in.py` pollt de Telegram Bot API, bewaart media in `vault/media/` en maakt een Markdown-concept met YAML-frontmatter in `vault/concepten/`. Elk bericht krijgt een afgerond `TELEGRAM_INBOUND` audit-event. Met `--auto-dispatch` ontstaan daarnaast publicatie-events voor uitsluitend actieve LinkedIn Pro-, Substack Pro- en Medium-routes.

## Modulaire Media Store

Media-adapters implementeren `plugins/media/base.py` en leveren genormaliseerde assets aan de SQLite-index. Het dashboard zoekt uitsluitend in die index. Lokale bestanden worden alleen via een gevalideerd asset-id geserveerd en moeten binnen de geregistreerde bronroot vallen. Een draftkoppeling wordt automatisch overgenomen wanneer dat concept later wordt ingepland.

## Worker-daemon

`daemon/worker.py` pollt continu met een configureerbaar interval. Plugins worden geïsoleerd als subprocess gestart. Een non-zero exitcode wordt vastgelegd; maximaal drie pogingen zijn toegestaan voordat het event atomisch naar de dead-letter queue verhuist.

Belangrijke omgevingsvariabelen:

- `PLUGIN_TIMEOUT_SECONDS`
- `LOG_LEVEL`
- `LOCAL_LLM_COMMAND`
- `LOCAL_LLM_MOCK`

## Folder-watchdog en Obsidian

`daemon/folder_watchdog.py` bewaakt `vault/uitgaand/`. Indien `watchdog` beschikbaar is, worden filesystem-events gebruikt; anders is er een veilige pollingfallback. Zowel nieuw aangemaakte als verplaatste `.md`-bestanden worden verwerkt.

Voorbeeldfrontmatter:

```yaml
---
topic: "Soevereine Automatisering"
publish_channel: "PUBLISH_MOCK"
platform: "linkedin"
---
```

De watcher leest metadata, verplaatst het bestand naar `vault/gepubliceerd/` onder een unieke naam en schrijft daarna een event waarvan `draft_file` naar het definitieve archiefpad wijst. Bij een databasefout wordt de bestandsverplaatsing teruggedraaid.

## Skills en veilige templating

`core/markdown_parser.py` leest YAML-frontmatter met `yaml.safe_load`. `required_inputs` wordt gevalideerd vóór generatie. Alleen gedeclareerde `{{placeholders}}` worden vervangen; ontbrekende, dubbele, samengestelde of niet-gedeclareerde waarden veroorzaken een fail-fast fout.

```yaml
---
required_inputs: [topic, research_context]
---

Schrijf over {{topic}} met deze context: {{research_context}}
```

## Betrouwbaarheid

- SQLite-transacties en busy timeouts.
- Atomische queueclaims.
- Begrensde foutlogs.
- Geen shell bij pluginexecutie.
- Unieke artifactnamen en atomische bestandswrites waar relevant.
- Screenshots bij browserautomatiseringsfouten.
- Read-only databaseverbinding voor het statusdashboard.
