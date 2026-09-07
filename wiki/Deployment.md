# Deployment

## Platform

Doelplatformen zijn Debian/Ubuntu Linux op ARM64, x86_64, CPU-only en NVIDIA
workstations. De units zijn templates en bevatten geen vaste gebruiker,
checkout of Python-pad.

Pas deze waarden aan wanneer de checkout op een ander absoluut pad of onder een andere gebruiker draait.

## Platform-neutrale runtime

De bootstrap ondersteunt ARM64/Raspberry Pi, x86_64, CPU-only en NVIDIA-hosts.
NVIDIA/CUDA, Ollama en Chromium zijn optionele capabilities; ontbrekende opties
worden als diagnostics gemeld. Voor deployment configureer je:

```bash
CORE_HOME=/opt/core-01
CORE_DATA=/var/lib/core-01
CORE_USER=core01
CORE_GROUP=core01
CORE_PYTHON=/opt/core-01/venv/bin/python3
```

`deploy/install_services.sh` rendert de systemd-templates met deze waarden en
gebruikt geen ontwikkelaarspad of vaste gebruiker.

## Bootstrap

```bash
chmod +x bootstrap_env.sh
./bootstrap_env.sh
```

De bootstrap installeert Flatpak/Obsidian en Python-systeempakketten, maakt de
runtime-directorystructuur onder `CORE_DATA` aan en corrigeert eigenaar- en
gebruikersrechten. Alleen source-controlled `vault/skills/` wordt nog door de
bootstrap aangemaakt; oude runtime-vaultmappen worden niet verwijderd of
opnieuw aangemaakt.

## Virtual environment

```bash
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install \
  playwright python-dotenv requests PyYAML watchdog
./venv/bin/playwright install chromium
```

## LinkedIn storage-state

```bash
mkdir -p config
./venv/bin/playwright codegen \
  --save-storage=config/linkedin_auth.json \
  https://www.linkedin.com/feed/
chmod 600 config/linkedin_auth.json
```

## Sessies koppelen vanuit Chrome

Start Chrome met remote debugging en een persistent profiel, open de gewenste
LinkedIn-, Substack- of Medium-tab en kies in het dashboard **Connect sessie**.
Rechtstreeks kan dezelfde flow met:

```bash
./venv/bin/python3 dashboard/authenticate.py \
  --platform substack \
  --auth-file config/substack_auth.json \
  --cdp-url http://127.0.0.1:9222
```

Sessies worden uitsluitend als storage-state opgeslagen met bestandsrechten `0600`.

Storage-state bevat sessiegeheimen. Deel of commit dit bestand niet. Bij een verlopen sessie moet het opnieuw worden gegenereerd.

## systemd

Units:

- `deploy/social-worker.service`
- `deploy/social-watchdog.service`
- `deploy/social-scheduler.service`
- `deploy/social-telegram-in.service`
- `deploy/social-health-check.service`

Installeren:

```bash
sudo ./deploy/install_services.sh
```

Het installatiescript kopieert de units naar `/etc/systemd/system/`, voert `systemctl daemon-reload` uit en gebruikt `systemctl enable --now` voor beide services.

De Telegram-unit wordt alleen automatisch gestart als `.env` een niet-lege `TELEGRAM_BOT_TOKEN` bevat; zonder credentials ontstaat dus geen herstartlus.

Status en logs:

```bash
systemctl status social-worker social-watchdog
journalctl -u social-worker -u social-watchdog -f
```

## Productiechecklist

- [ ] `CORE_DATA/db/events.db` is geïnitialiseerd en schrijfbaar voor de servicegebruiker.
- [ ] Plugins zijn geregistreerd en event-routes zijn actief.
- [ ] `CORE_PYTHON` verwijst naar de venv van deze installatie.
- [ ] `.env` bevat uitsluitend noodzakelijke configuratie en heeft beperkte rechten.
- [ ] `config/linkedin_auth.json` heeft mode `0600` wanneer live LinkedIn actief is.
- [ ] Substack/Medium-sessies zijn via CDP gekoppeld wanneer die kanalen actief zijn.
- [ ] `CORE_DATA/` en de benodigde logmappen zijn schrijfbaar voor `CORE_USER`.
- [ ] Mocktests slagen vóór live publicatie wordt ingeschakeld.
- [ ] Dead-letter queue en screenshots worden operationeel gemonitord.

## Optionele analytics

Analytics is niet vereist voor een gezonde installatie. Registreer de provider
alleen wanneer je metingen wilt verzamelen:

```bash
./venv/bin/python3 plugins/analytics/website_analytics.py \
  --register --db "$CORE_DATA/db/events.db"
```

Voor Plausible zet je `PLAUSIBLE_SITE_ID`, `PLAUSIBLE_API_KEY` en eventueel
`PLAUSIBLE_API_BASE_URL` in `.env` of de serviceomgeving. Sleutels komen nooit
in Git, logs of raw payloads. Een `ANALYTICS_COLLECT` event met
`mode: SIMULATED` is geschikt voor offline testen; cadence (bijvoorbeeld 24 uur,
7 dagen en 30 dagen) blijft een expliciete schedulerkeuze. Providerproblemen
worden als `AUTH_REQUIRED`, `RATE_LIMITED` of `FAILED` zichtbaar zonder de
basisdeployment ongezond te maken. `REAL` is de standaardmodus voor dashboard
en evergreen; gebruik `mode=SIMULATED` alleen voor expliciete fixturetests. De
standaarddatabase van de analytics-plugin is altijd `CORE_DATA/db/events.db`;
`--db` blijft beschikbaar voor diagnostiek.

LinkedIn Analytics is optioneel en gebruikt dezelfde Playwright-opslag als de
LinkedIn Pro publisher: `CORE_DATA/sessions/linkedin_auth.json` (mode 600), of
het pad uit `LINKEDIN_AUTH_PATH`. Registreer beide capability-rijen via de
dispatcher en schakel `LinkedIn Analytics` uit als geen sessie beschikbaar is.
Een ontbrekende sessie geeft `AUTH_REQUIRED`/`BLOCKED_AUTH` en maakt de basis-
deployment niet ongezond. Gebruik lifetime-snapshots; latere geplande
collecties (bijvoorbeeld na 24 uur, 7 dagen en 30 dagen) onderscheiden zich
door `collected_at`. Handmatige collectie gebruikt `POST /api/analytics/collect`
met `provider: "linkedin"`; alleen publicatie-specifieke LinkedIn-permalinks
worden geaccepteerd, niet feed- of activity-overzichtspagina's.

## Lokaal dashboard

De web-UI bindt standaard uitsluitend aan `127.0.0.1`:

```bash
./venv/bin/pip install -r dashboard/requirements.txt
./venv/bin/python3 dashboard/run.py --db "$CORE_DATA/db/events.db"
```

Open daarna `http://127.0.0.1:8080`. Gebruik `--host 0.0.0.0` alleen achter
een vertrouwde firewall of reverse proxy met authenticatie.

Controleer de installatie met (de database uit `CORE_DATA` is de standaard):

```bash
./venv/bin/python3 scripts/deployment_doctor.py
```
## Writer-assets

De Flask-dashboardruntime heeft geen Node nodig. Bouw de lokale Milkdown-assets
eenmalig tijdens deployment of development:

```bash
npm ci
npm run build
```

De gegenereerde `dashboard/static/editor.bundle.*` wordt lokaal door Flask
geserveerd; er is geen Milkdown CDN. Runtime-editorbestanden staan in
`$CORE_DATA/concepts` en `$CORE_DATA/outgoing`. Preview en voer de copy-only
migratie uit voor bestaande legacybestanden:

```bash
python3 scripts/migrate_runtime_data.py --dry-run
python3 scripts/migrate_runtime_data.py
```
