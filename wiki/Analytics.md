# Analytics & Feedback Loop

## Doel en architectuur

Core-01 meet optioneel wat er na publicatie gebeurt:

```text
publication_attempts → ANALYTICS_COLLECT → provider
  → analytics_snapshots/analytics_metrics → ANALYTICS_AGGREGATE
  → content_performance/content_feedback → CONTENT_PERFORMANCE_UPDATED
```

De worker en SQLite blijven de enige control plane. Er is geen tweede queue,
scheduler of database. Analytics-feedback is adviserend en republished nooit
automatisch.

## Provider en configuratie

`plugins/analytics/website_analytics.py` bevat het kleine providercontract, een
Plausible-adapter en een deterministische `SimulatedProvider`. Registreer hem
met:

```bash
./venv/bin/python3 plugins/analytics/website_analytics.py --register --db "$CORE_DATA/db/events.db"
```

Plausible-configuratie komt uit de omgeving: `PLAUSIBLE_SITE_ID`,
`PLAUSIBLE_API_KEY` en optioneel `PLAUSIBLE_API_BASE_URL`. Secrets blijven
buiten Git en worden nooit in raw providerdata of logs opgeslagen.

## Events en windows

`ANALYTICS_COLLECT` accepteert `publication_attempt_id`, `canonical_url`,
`external_id`, `channel`, `window` (`24h`, `7d`, `30d`, `lifetime`) en optioneel
`mode: SIMULATED` met `fixture.metrics`. `ANALYTICS_AGGREGATE` maakt een
performance-record en emitteert compact `CONTENT_PERFORMANCE_UPDATED`.

Herhaalde snapshots worden gededupliceerd op provider, external id, tijdvenster
en source hash; verschillende meetmomenten blijven bewaard. Niet-ondersteunde
metrics blijven ontbrekend/`NULL`, terwijl gemeten nulwaarden nul zijn.

## Attributie en veiligheid

Attributie gebruikt een ledger-id of exacte canonical URL. Een onbekende URL
wordt niet fuzzy gekoppeld en blijft `UNATTRIBUTED`. Raw JSON is begrensd en
bevat geen auth headers. Providerverzoeken hebben een timeout en veilige
statusmapping: `AUTH_REQUIRED`, `RATE_LIMITED`, `FAILED`, `COMPLETED` of
`SIMULATED`.

## Dashboard/API

- `GET /api/analytics/publications`
- `GET /api/analytics/publications/<id>`
- `GET /api/analytics/snapshots?provider=&channel=&metric=&limit=`
- `GET /api/analytics/providers`

De endpoints tonen latest performance, historische snapshots, rates,
attributiestatus en provider health. Bestaande `/api/analytics` file-artifacts
blijven backwards compatible.

## Evergreen en feedback

`content_performance` bevat transparante componenten (views, clicks,
engagements, shares, saves, engagement- en click-rate). `content_feedback`
legt een uitlegbare score vast. Bestaande evergreen-proposals kunnen deze
gegevens gebruiken, maar verzamelen of aggregeren veroorzaakt geen publicatie.

## Simulatie en troubleshooting

Gebruik een fixture-event voor offline ontwikkeling. Controleer de queue- en
pluginroute met `scripts/system_status.py`, registreer de provider opnieuw als
de route ontbreekt, en bekijk `/api/analytics/providers` voor
`AUTH_REQUIRED`, `RATE_LIMITED` of `FAILED`. Geen provider actief is een geldige,
gezonde configuratie.
