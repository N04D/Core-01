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

Dashboarduploads worden atomisch opgeslagen in `vault/media/` en als bron `vault-uploads` in dezelfde Media Store-index geregistreerd. De backend controleert zowel extensie als bestands-signatuur voor PNG, JPG, GIF, WebP, MP4, WebM en MOV. Een databasefout verwijdert het zojuist opgeslagen bestand weer, zodat index en filesystem consistent blijven.

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

## Lokale RAG en redactie-loop

`core/rag_index.py` splitst Markdown uit de Obsidian-vault in overlappende chunks en bewaart genormaliseerde, deterministische feature-hashing-vectoren in SQLite. Retrieval gebruikt cosinusovereenkomst en werkt zonder externe vectorservice of native ML-library.

De AI-generator ververst de index incrementeel en haalt relevante vaultchunks op. Daarna doorloopt ieder concept drie lokale rollen: **Schrijver**, **Factchecker** en **Redacteur**. De Factchecker moet expliciet `APPROVED` geven. Alleen de eindtekst van de Redacteur wordt atomisch in `vault/concepten/` opgeslagen; een afwijzing of lege roloutput laat het event falen.

## Kanaalvarianten en evergreen-content

De Master Syndication Suite gebruikt één centraal essay als bron voor drie afzonderlijke, redactioneel gecontroleerde `AI_GENERATION`-events: een korte LinkedIn-post, een long-form Substack-artikel en een SEO-geoptimaliseerd Medium-artikel. `content_variants` bewaart de herkomst en het generatie-event; ieder publicatie-event ontvangt uitsluitend zijn eigen variant.

`core/evergreen.py` normaliseert analytics naar views, likes, comments, shares en engagement en berekent een configureerbare score. Hoogpresterende posts worden in `evergreen_posts` gemarkeerd. Zodra `eligible_after` is bereikt, maakt de scheduler één `evergreen_proposals`-record met een LLM-herschrijfbriefing. Dit is bewust een voorstel en geen automatische herpublicatie.

## Betrouwbaarheid

De session-health daemon controleert actieve LinkedIn-, Substack- en Medium-publishers periodiek via hun Playwright storage-state en een positieve headless accountindicator. Statuswijzigingen naar `AUTH_REQUIRED` produceren één kritisch `SYSTEM_AUTH_REQUIRED` audit-event en één ongelezen dashboardnotificatie. Met `TELEGRAM_NOTIFICATION_CHAT_ID` en `TELEGRAM_BOT_TOKEN` wordt dezelfde overgang ook naar Telegram gestuurd.

- SQLite-transacties en busy timeouts.
- Atomische queueclaims.
- Begrensde foutlogs.
- Geen shell bij pluginexecutie.
- Unieke artifactnamen en atomische bestandswrites waar relevant.
- Screenshots bij browserautomatiseringsfouten.
- Read-only databaseverbinding voor het statusdashboard.

## Broncode versus runtime-data

De repository bevat uitsluitend broncode, prompts, skills, templates en fixtures.
Mutable data wordt centraal opgelost door `core/paths.py`:

```text
CORE_ROOT  = checkout/source repository
CORE_DATA  = runtime root (default: <checkout>/runtime)
```

Daaronder staan `db/`, `media/`, `analytics/`, `research/`, `concepts/`,
`published/`, `logs/`, `sessions/` en `tmp/`. Auth storage-state is altijd
0600 en staat onder `sessions/`. `scripts/migrate_runtime_data.py` kopieert de
oude `vault/`- en `db/`-inhoud zonder bestaande bestemmingen of bronbestanden te
overschrijven. Runtimepaden zijn door `.gitignore` uitgesloten.

## NightCafe als normaal event

De FastAPI- en Flask-adapters voor NightCafe schrijven uitsluitend een
`NIGHTCAFE_GENERATE`-event met een JSON-payload naar `events_queue`. Ze houden
geen in-memory jobregister of uitvoerende achtergrondthread bij. De worker claimt
het event, start `plugins/media/nightcafe_automation.py` en schrijft status/resultaat
duurzaam terug; een API-restart verliest dus geen jobstatus.

## Publication reconciler

`core/reconciler.py` selecteert `SUBMITTED`/`UNKNOWN` ledgerrecords. Zonder
channel-specifiek sterk bewijs wordt nooit opnieuw gepubliceerd: de poging gaat
naar `NEEDS_OPERATOR`. Een channel adapter mag alleen `CONFIRMED` of `FAILED`
teruggeven met bewijs (platform-ID/URL of een betrouwbare contentmatch). Het
dashboard exposeert unresolved attempts en biedt een expliciete operator-resolve
actie.

## Sessies en dashboard-authenticatie

`dashboard/authenticate.py` kan via Chrome DevTools Protocol (standaard
`http://127.0.0.1:9222`) een bestaande tab voor LinkedIn, Substack of Medium
overnemen. De helper zoekt op domein, controleert bekende sessiecookies en schrijft
Playwright storage-state atomisch met mode `0600` naar `config/*_auth.json`.
Het dashboard toont de actuele bestandsstatus live; de Home-actie **Connect sessie**
opent eerst de plugininstellingen en voorkomt dubbele loginprocessen.
