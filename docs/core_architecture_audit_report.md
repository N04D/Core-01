# Core Architecture & Backend Audit

**Datum:** 27 augustus 2026  
**Scope:** `core/`, `daemon/`, `plugins/`, `playbooks/`, `deploy/` en de SQLite-database `db/events.db`  
**Werkwijze:** statische code-review en uitsluitend read-only runtimecontroles. Er zijn geen implementatiebestanden, configuraties, services of databasegegevens gewijzigd.

## Managementsamenvatting

De architectuur heeft een bruikbaar lokaal fundament: SQLite draait in WAL-mode, queueclaims zijn atomair, de scheduler dispatcht binnen één transactie, plugins worden zonder shell uitgevoerd en de belangrijkste langlopende daemons hebben signal handling en systemd-restartbeleid. De live database doorstond `PRAGMA integrity_check` met resultaat `ok`.

De productiebetrouwbaarheid is echter nog niet voldoende voor onbeheerd publiceren. De vier belangrijkste risico's zijn:

1. Een workercrash kan een event permanent in `PROCESSING` achterlaten, omdat leases, heartbeats en een reaper ontbreken.
2. Worker en plugin schrijven allebei de eindstatus. Daardoor is niet eenduidig welk proces eigenaar is van de event-state.
3. Publishers hebben geen publicatie-idempotentie of bevestigde platform-ID. Een crash na de echte klik maar vóór de database-update kan bij retry een dubbele publicatie veroorzaken.
4. De Pro-publishers behandelen ontbrekende authenticatie als een veilige mock, maar zetten event-busitems vervolgens op `COMPLETED`. Orchestrators kunnen dit ten onrechte als een geslaagde publicatie rapporteren.

**Auditconclusie:** geschikt voor lokale test- en mockworkflows; vóór productiegebruik zijn de High-impact verbeteringen hieronder noodzakelijk.

## Huidige architectuur

De in de opdracht genoemde generieke mappen heten in deze codebase concreet:

- Core en database-initialisatie: `core/` en `db/events.db`
- Daemons: `daemon/`
- Publishers: `plugins/channels/`
- AI en I/O: `plugins/ai/` en `plugins/io/`
- Orchestratie: `playbooks/`
- Services: `deploy/`

De hoofdflow is:

```text
Watchdog / Scheduler / Playbook / Telegram
                  |
                  v
          SQLite events_queue
                  |
          atomische workerclaim
                  |
                  v
       route + actieve plugin lookup
                  |
                  v
       subprocess naar lokale plugin
          |                   |
       succes             retry (max. 3)
          |                   |
      COMPLETED             DLQ
```

## Bevestigde sterke punten

- `core/setup_database.py` activeert WAL en een busy timeout tijdens initialisatie. De bestaande database rapporteert `journal_mode=wal`, `synchronous=FULL` en `integrity_check=ok`.
- `daemon/worker.py` claimt het oudste `PENDING`-event met `BEGIN IMMEDIATE` en `UPDATE ... RETURNING`. Meerdere workers kunnen daardoor niet hetzelfde beschikbare event claimen.
- `daemon/scheduler.py` selecteert, enqueue't en markeert geplande items als `DISPATCHED` binnen één write-transactie.
- Queuepayloads worden als JSON gevalideerd door SQLite-checkconstraints.
- Pluginprocessen worden als argumentlijst gestart, zonder `shell=True`; command-injectie via de shell wordt daarmee vermeden.
- Worker, scheduler, watchdog en health checker gebruiken begrensde polling-intervallen. Scheduler, watchdog en health checker verwerken `SIGINT` en `SIGTERM` met een stop-event.
- De DLQ-verplaatsing bij de derde mislukking gebeurt transactioneel: invoegen in `dead_letter_queue` en verwijderen uit `events_queue` vormen één commit.
- Authbestanden worden structureel gecontroleerd en de Pro-plugins vereisen bestandsmode `0600`.
- De LLM-runner gebruikt geen shell, kent time-outs en schrijft gegenereerde Markdown atomisch met `fsync` en rename.
- De systemd-units gebruiken absolute paden, de project-venv, `Restart=always`, `RestartSec=5`, `NoNewPrivileges`, `PrivateTmp` en een restrictieve `UMask` voor de kernservices.

## Runtimebeeld tijdens de audit

- Database-integriteit: `ok`
- WAL-mode: actief
- Queue: 27 `COMPLETED`, 2 `FAILED`, 3 `PENDING`, 0 `PROCESSING`
- Dead-letter queue: 0 items
- De onderzochte services waren op het auditmoment niet actief.
- De unitbestanden waren niet geïnstalleerd onder de bevraagde systemd-unitnamen; `systemctl is-enabled` meldde dat `social-worker.service` niet bestond. Dit is een deploymentstatus, geen fout in de unitbestanden in `deploy/`.
- `systemd-analyze verify` rapporteerde geen projectspecifieke unitfouten. De getoonde meldingen over `netplan-ovs-cleanup` en `snapd` kwamen uit het hostsysteem.

Deze momentopname bewijst niet dat events onder belasting probleemloos worden verwerkt; er is in deze read-only audit bewust geen queue-event geïnjecteerd.

## Bevindingen — High impact

### H1 — `PROCESSING`-events kunnen permanent vastlopen

De worker commit de claim voordat de plugin wordt uitgevoerd. Dat is correct voor korte transacties, maar de rij bevat geen `claimed_by`, `lease_until`, `heartbeat_at` of claimtoken. Bij een crash, reboot, OOM-kill of geforceerde stop blijft de rij onbeperkt `PROCESSING`. Systemd herstart de worker, maar de nieuwe worker selecteert uitsluitend `PENDING`.

**Advies:** voeg leasekolommen en een unieke claimtoken toe. Laat de worker periodiek een heartbeat vernieuwen en laat een reaper verlopen leases gecontroleerd terugzetten naar `PENDING`. Gebruik een maximale runtime per eventtype en log elke recovery. Statusupdates moeten de claimtoken in de `WHERE`-clausule controleren.

### H2 — Worker en plugins zijn beide eigenaar van de eventstatus

De worker zet een event op `PROCESSING` en markeert het na subprocesssucces als `COMPLETED`; meerdere plugins zetten hetzelfde event zelf eveneens op `COMPLETED` of `FAILED`. Een plugin kan dus `FAILED` schrijven en non-zero afsluiten, waarna de worker een retry uitvoert, of `COMPLETED` schrijven waarna een procesprobleem alsnog een foutpad activeert. Dit maakt state-transities, retries en observability ambigu.

**Advies:** kies één contract. De aanbevolen variant is dat uitsluitend de worker queue-state beheert. Plugins retourneren een gestructureerd result-envelope via stdout of een aparte `event_results`-tabel en wijzigen `events_queue` niet. Leg toegestane state-transities centraal vast en controleer altijd het aantal gewijzigde rijen.

### H3 — Geen exactly-once-effect of idempotentie voor echte publicaties

Een publisher kan op “Publish” klikken en daarna crashen voordat `COMPLETED` wordt opgeslagen. De worker probeert het event opnieuw, waardoor dezelfde content dubbel kan verschijnen. De plugins slaan geen idempotency key, platform-post-ID, bevestigings-URL of `SUBMITTED/CONFIRMED/UNKNOWN`-fase op. Na de klik wordt succes bovendien niet overal expliciet bevestigd met een platformrespons, toast of nieuwe URL.

**Advies:** introduceer een `publication_attempts`-ledger met unieke `(event_id, channel)`-sleutel, contenthash, target, attempt, platform-ID/URL en status `PREPARED`, `SUBMITTED`, `CONFIRMED`, `UNKNOWN` of `FAILED`. Controleer na submit een platformbewijs. Laat een `UNKNOWN`-poging eerst reconciliëren voordat retry opnieuw publiceert.

### H4 — `AUTH_REQUIRED` wordt als `COMPLETED` gerapporteerd

LinkedIn Pro, Substack Pro en Medium bieden bij ontbrekende auth een veilige fallback met `status=AUTH_REQUIRED` en `published=false`, maar de eventafhandeling kan de queue als `COMPLETED` markeren. Playbooks zien daardoor een groene eindstatus hoewel niets is gepubliceerd. Het queueschema accepteert `AUTH_REQUIRED` ook niet als status.

**Advies:** voeg een expliciete terminale of wachtstatus toe, bijvoorbeeld `BLOCKED_AUTH`, of modelleer blokkades in een aparte kolom. De plugin moet non-zero of een gestructureerd blocked-resultaat retourneren; de worker mag dit niet als retrybare technische fout of als succes behandelen. Toon mock/dry-run als afzonderlijke uitkomst (`SIMULATED`) en nooit als echte publicatie.

### H5 — Retrybeleid kan queue starvation en herhaalde side effects veroorzaken

Retries zijn direct, zonder backoff, jitter of `next_attempt_at`. Het oudste falende event wordt daardoor snel opnieuw geclaimd en kan nieuw werk verdringen. De foutclassificatie onderscheidt transient, permanent, auth, validatie en unknown niet.

**Advies:** voeg `next_attempt_at`, foutcategorie en configureerbare retry-policy per eventtype toe. Gebruik exponentiële backoff met jitter. Validatie- en authfouten horen niet automatisch drie keer uitgevoerd te worden. Combineer dit met idempotentie en een samengestelde queue-index.

### H6 — Telegram-invoer is zonder allowlist standaard open

Als `TELEGRAM_ALLOWED_CHAT_IDS` leeg is, accepteert de listener alle chats. In combinatie met automatische dispatch kan een externe afzender publicatie-events laten creëren. Daarnaast ontbreekt een unieke Telegram update/message-ID in de database: een crash na commit maar vóór het opslaan van de offset kan dezelfde update opnieuw verwerken.

**Advies:** fail closed: vereis een expliciete chat-allowlist voordat auto-dispatch mogelijk is. Sla `update_id` en `message_id` op met een UNIQUE-constraint en commit de verwerking idempotent. Valideer gedownloade media op MIME/signature, extensie en grootte.

## Bevindingen — Medium impact

### M1 — Ontbrekende queue- en schedulerindexen

De claimquery filtert op status en sorteert op `created_at, id`; de scheduler filtert op status en `scheduled_time`. Hiervoor zijn geen expliciete indexen gevonden. Bij groei nemen de scantijd en de duur van `BEGIN IMMEDIATE` toe, waardoor andere writers langer wachten.

**Advies:** voeg minimaal partiële of samengestelde indexen toe op `events_queue(status, created_at, id)` en `scheduled_events(status, scheduled_time, id)`. Onderbouw de keuze met `EXPLAIN QUERY PLAN` en een concurrerende loadtest.

### M2 — SQLite-instellingen zijn niet centraal per verbinding afgedwongen

WAL blijft databasebreed actief, maar `foreign_keys` is connection-scoped en stond op een verse auditverbinding uit. Busy timeouts verschillen tussen geïnitialiseerde en standaardverbindingen. Er is geen gedeelde connection factory die alle pragmas en row factories afdwingt.

**Advies:** centraliseer databaseconnecties en stel per connectie ten minste `foreign_keys=ON`, een consistente `busy_timeout` en observabele transactionele defaults in. Voeg foreign keys toe waar de levenscyclus dat toelaat, bijvoorbeeld route naar plugin en gerelateerde records naar event/workflow.

### M3 — DLQ heeft opslag maar geen operationele lifecycle

De DLQ ontvangt definitief mislukte items, maar er is geen expliciete redrive-, acknowledge-, quarantine- of retentionflow. De originele ID wordt hergebruikt; toekomstige herstelacties kunnen botsen met nieuwe IDs of historie vertroebelen.

**Advies:** geef DLQ-items een eigen sleutel plus `original_event_id`, foutcategorie, first/last failure, attempt history en payloadhash. Bouw een gecontroleerde redrive met nieuwe event-ID en causation-link. Voeg dashboardalerts en retentionbeleid toe.

### M4 — LLM-time-outs zijn onderling inconsistent

De editorloop kan drie opeenvolgende modelcalls doen. Elke call mag standaard circa 300 seconden duren, terwijl de worker het volledige pluginproces standaard na circa 300 seconden beëindigt. Een geldige writer/factchecker/editor-run kan dus door de outer timeout worden afgebroken. `communicate()` buffert volledige stdout/stderr in geheugen en bij timeout wordt alleen het directe proces gedood; descendants in de nieuwe processessie kunnen blijven draaien.

**Advies:** definieer één end-to-end deadline en verdeel die over rollen. Kill bij timeout de volledige process group, begrens output, log welke rol faalde en maak role-timeouts configureerbaar. Voeg cancellation propagation toe bij systemd-stop.

### M5 — RAG-indexering in het publicatiepad heeft onbegrensde latency

Automatische RAG-refresh kan tijdens iedere generatie de vault opnieuw doorlopen en schrijven. Zo concurreert indexering met queuewriters en maakt het de pluginruntime onvoorspelbaar. Retrieval laadt alle chunks/vectoren en scoort in Python lineair; dit schaalt slecht. Context en prompt hebben geen expliciet tokenbudget.

**Advies:** verplaats incrementele indexering naar een aparte daemon/job op bestandswijzigingen. Gebruik een indexgeneratie/snapshot, batchtransacties, begrens chunks op tokenbudget en meet indexleeftijd. Voor grotere vaults is een echte ANN/vectorbackend of SQLite-vectoruitbreiding passender.

### M6 — Redactiegoedkeuring is tekstueel en prompt-injectiegevoelig

De factchecker wordt goedgekeurd als de output de tekenreeks `FACTCHECK_STATUS: APPROVED` bevat. Die tekst kan geciteerd of door broncontext geïnjecteerd worden. Vault- en webinhoud worden als promptcontext vertrouwd zonder duidelijke trust boundary.

**Advies:** verlang strikt gestructureerde JSON-output met schemavalidatie, afzonderlijk beslisveld en evidence-referenties. Behandel opgehaalde tekst als onbetrouwbare data, scheid instructies van context en laat onzekere claims blokkeren of escaleren.

### M7 — Playwright-selectors en sessievalidatie zijn fragiel

Er zijn nuttige selectorfallbacks, maar meerdere selectors zijn breed, tekst- en taalafhankelijk (`contenteditable`, generieke Publish-knoppen, generieke file-inputs). Authvalidatie controleert vooral JSON-vorm en rechten; cookie-domein, naam en verloop worden niet vooraf betrouwbaar gevalideerd. Analytics helpers kunnen selectorfouten omzetten in lege data en toch succes opslaan.

**Advies:** gebruik waar mogelijk stabiele accessible roles, labels en nauwe container-scoping. Versioneer selectorprofielen per platform, sla bij fouten screenshot plus gesaniteerde DOM/trace op en valideer expliciete post-submit-signalen. Classificeer netwerk-, DOM- en authfouten apart. Laat lege analytics geen succes zijn zonder completeness-indicator.

### M8 — Subprocessshutdown kan browsers en modellen verweesd achterlaten

De worker start plugins in een nieuwe processessie, maar heeft zelf geen expliciete `SIGTERM`-handler. Bij systemd-stop kan de worker sterven terwijl het kind buiten de oorspronkelijke processgroep verder draait. De LLM-runner kent hetzelfde patroon voor het modelproces.

**Advies:** implementeer graceful drain, bewaar child-PID/process-group, stuur eerst `SIGTERM` en na een grace period `SIGKILL` naar de volledige groep. Configureer systemd `KillMode` bewust en test stop/restart tijdens browser- en modelwerk.

### M9 — Watchdog filesystem- en databaseactie zijn niet atomair

Een bestand wordt naar `vault/gepubliceerd/` verplaatst en daarna wordt het event ingevoegd. Bij een crash tussen deze acties kan het bestand gearchiveerd zijn zonder event. Bij een databasefout probeert de code terug te bewegen, maar dat dekt geen procescrash. Bovendien suggereert `gepubliceerd` publicatiesucces terwijl alleen intake heeft plaatsgevonden.

**Advies:** gebruik een staging/claimed-directory en een persistent intake-record met contenthash/idempotency key. Archiveer pas na bevestigd publicatiesucces, of noem de tussenmap `verwerkt`. Plaats ongeldige poison files in een fout/quarantainemap.

### M10 — Playbooks zijn niet duurzaam hervatbaar

Workflows bestaan hoofdzakelijk uit polling in het aanroepende proces. Er is geen workflow-run/DAG-tabel met correlation- en causation-ID's, checkpoints of compensatie. Een playbooktimeout annuleert het queue-event niet; het kan later alsnog publiceren nadat de gebruiker een timeout zag. Niet alle playbooks controleren de DLQ bij een verdwenen event.

**Advies:** modelleer workflows persistent, koppel child events aan een run en voeg cancellation/deadline toe. Een timeout moet een expliciete keuze geven: blijven volgen, veilig annuleren vóór submit of status `UNKNOWN` reconciliëren.

### M11 — Scheduler mist audit- en idempotentievelden

`scheduled_events` bewaart niet welk queue-event uit een planning ontstond en heeft geen timestamps, dispatchfout of idempotency key. De tijdvergelijking steunt op lexicografisch vergelijkbare ISO-UTC-strings.

**Advies:** normaliseer alle tijden naar UTC, valideer formaat en voeg `dispatched_event_id`, `created_at`, `updated_at`, `last_error` en een unieke dispatchsleutel toe. Leg annulering versus reeds geclaimde dispatch expliciet vast.

### M12 — Firecrawl accepteert onbeperkte doel-URL's en output

De scraper valideert `http(s)`, maar voorkomt geen loopback-, link-local- of private netwerkdoelen. Als eventinjectie ooit extern bereikbaar wordt, ontstaat SSRF-risico. Response- en Markdowngrootte zijn niet begrensd en transient retries ontbreken.

**Advies:** pas een domein/IP-policy toe na DNS-resolutie, blokkeer private en metadata-adressen tenzij expliciet toegestaan, begrens responsegrootte en voeg gecontroleerde retry/backoff toe.

## Bevindingen — Low impact / onderhoudbaarheid

### L1 — Observability is verspreid

Logs zijn leesbaar, maar niet consequent gestructureerd en missen een vaste workflow-ID, attempt-ID, claimtoken, duur, pluginversie en platformresultaat. Er zijn geen kernmetrics voor queue age, retry rate, stuck leases, DLQ-groei of publish confirmation latency.

**Advies:** voeg JSON-logging of consistente key-valuevelden, correlation IDs en eenvoudige metrics/health endpoints toe. Vermijd payload- en authdata in logs.

### L2 — Screenshots kunnen gevoelige data bewaren

Foutscreenshots kunnen feeds, profielen, concepten en persoonsgegevens bevatten. Er is geen zichtbaar retentie-, redacteer- of quota-beleid.

**Advies:** forceer private permissies, stel retentie en maximumopslag in, redacteer waar mogelijk en maak verzamelen configureerbaar. Neem screenshots nooit op in publieke vault-sync zonder waarschuwing.

### L3 — Service-installatie is hostgebonden

Units bevatten bewust absolute paden en gebruiker `infra`, maar zijn daardoor niet portable. `Restart=always` kan bij permanente configuratiefouten een restartloop veroorzaken. De installer voert geen schema/preflight, unit-verificatie of schrijfrechtentest uit en rapporteert de Telegram-unit niet uniform.

**Advies:** render units vanuit een veilig installatiescript, valideer gebruiker/paden/venv/database, gebruik `systemd-analyze verify`, voeg StartLimit-instellingen en `ExecStartPre`-checks toe. Versterk sandboxing stapsgewijs met expliciete writable paths, rekening houdend met Chromium.

### L4 — Artifact en database-update zijn niet één transactie

LLM-, Firecrawl- en publisherflows kunnen een bestand schrijven en daarna falen bij de DB-update, waardoor verweesde of dubbele artifacts ontstaan.

**Advies:** schrijf eerst naar staging, registreer artifactmetadata en finalize atomisch waar mogelijk. Gebruik contenthashes en een periodieke reconciler voor orphaned files.

## Aanbevolen uitvoeringsvolgorde

### Fase A — Veiligheid en correcte eventsemantiek

1. Maak de worker exclusief eigenaar van queue-status.
2. Voeg leases, claimtokens, heartbeat en stuck-event recovery toe.
3. Modelleer `BLOCKED_AUTH`, `SIMULATED` en foutcategorieën correct.
4. Voeg publicatieledger, idempotency keys en post-submit-reconciliatie toe.
5. Zet Telegram standaard dicht zonder allowlist.

### Fase B — Betrouwbaarheid onder storing en belasting

1. Voeg backoff/jitter, `next_attempt_at` en queue-indexen toe.
2. Harmoniseer LLM- en workerdeadlines en beëindig hele processgroepen.
3. Maak workflows persistent en annuleerbaar.
4. Maak watchdog-intake crash-safe en schedulerdispatch traceerbaar.
5. Centraliseer SQLite-connectiebeleid en voer concurrency/loadtests uit.

### Fase C — Platformrobustheid en operations

1. Versterk Playwright-selectors, sessiechecks en publish-confirmatie.
2. Verplaats RAG-refresh uit het requestpad en voeg tokenbudgetten toe.
3. Bouw DLQ-redrive, metrics, alerts en artifactreconciliatie.
4. Hard systemd-units verder uit en voeg deploymentpreflight toe.

## Minimale acceptatietests vóór live publicatie

- Worker wordt hard beëindigd na claim; event wordt na lease-expiry exact één keer herstelbaar.
- Worker wordt beëindigd direct vóór en direct na de platformklik; reconciliatie voorkomt dubbele publicatie.
- Twee of meer workers claimen onder gelijktijdige belasting geen dubbel event en blijven binnen een gemeten lockbudget.
- Ontbrekende/verlopen auth resulteert zichtbaar in `BLOCKED_AUTH`, nooit in `COMPLETED`.
- Transient netwerkfout gebruikt backoff; permanente validatiefout gaat zonder nutteloze retries naar quarantine/DLQ.
- `SIGTERM` tijdens Playwright en LLM laat geen browser-, model- of childprocessen achter.
- Watchdogcrashes op iedere grens tussen move en enqueue verliezen noch dupliceren input.
- Telegram replay van hetzelfde `update_id` creëert maximaal één intake en één workflow.
- LLM-editorloop respecteert één end-to-end deadline en een begrensd context/outputbudget.
- DLQ-redrive behoudt oorspronkelijke historie en veroorzaakt geen dubbele externe side effect.

## Beslispunt

Er zijn tijdens deze audit geen fixes doorgevoerd. Aanbevolen is om na expliciet akkoord te starten met **Fase A**, te beginnen bij H1/H2/H4 (event-state en leases) en daarna H3 (publicatie-idempotentie). Deze volgorde voorkomt dat verdere functionaliteit voortbouwt op ambigue succes- en retrysemantiek.
