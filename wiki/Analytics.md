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
performance-record en emitteert compact `CONTENT_PERFORMANCE_UPDATED`. Website
Analytics registreert alleen de twee input-events; `Analytics Feedback /
Evergreen Feedback` consumeert het output-event, zodat geen feedback-loop naar
de collector ontstaat.

Herhaalde snapshots worden gededupliceerd op provider, external id, tijdvenster
en source hash; verschillende meetmomenten blijven bewaard. Niet-ondersteunde
metrics blijven ontbrekend/`NULL`, terwijl gemeten nulwaarden nul zijn.

Attributie-identiteit volgt deze volgorde: expliciete `external_id`,
`platform_id`, exacte canonical URL, `publication:<id>`, en pas daarna een
deterministische `unattributed:<hash>`. Een expliciet onbekende publication-id
is een fout en wordt niet stil teruggebracht tot een ongeattribueerde meting.

`REAL` en `SIMULATED` zijn volledig gescheiden. Aggregatie gebruikt standaard
`REAL`; simulated performance/feedback blijft zichtbaar voor tests maar wordt
nooit meegenomen in `evergreen_posts` of productie-editorial beslissingen.

## Attributie en veiligheid

Attributie gebruikt een ledger-id of exacte canonical URL. Een onbekende URL
wordt niet fuzzy gekoppeld en blijft `UNATTRIBUTED`. Raw JSON is begrensd en
bevat geen auth headers. Providerverzoeken hebben een timeout en veilige
statusmapping: `AUTH_REQUIRED`, `RATE_LIMITED`, `FAILED`, `COMPLETED` of
`SIMULATED`.

Grote providerresponses worden als geldige JSON-diagnostiek begrensd op 100 KB.
De hash wordt over de volledige gesaneerde response berekend; token-, cookie-,
password- en authorizationvelden worden niet opgeslagen.

## Dashboard/API

- `GET /api/analytics/publications`
- `GET /api/analytics/publications/<id>`
- `GET /api/analytics/snapshots?provider=&channel=&metric=&limit=`
- `GET /api/analytics/providers`
- `POST /api/analytics/collect`

De endpoints tonen latest performance, historische snapshots, rates,
attributiestatus en provider health. Zonder `mode` tonen performance- en
snapshot-endpoints alleen `REAL`; gebruik `mode=SIMULATED` of `mode=ALL` voor
tests. De legacy `/api/analytics` artifact-route ondersteunt dezelfde modefilter
en blijft backwards compatible.

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
