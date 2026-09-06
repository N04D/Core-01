# Deployment

## Platform

Doelplatform: Debian/Ubuntu Linux op ARM64 (Raspberry Pi 5). De units in deze checkout zijn gegenereerd voor:

- Gebruiker/groep: `infra:infra`
- Projectroot: `/home/infra/dev`
- Python: `/home/infra/dev/venv/bin/python3`

Pas deze waarden aan wanneer de checkout op een ander absoluut pad of onder een andere gebruiker draait.

## Bootstrap

```bash
chmod +x bootstrap_env.sh
./bootstrap_env.sh
```

De bootstrap installeert Flatpak/Obsidian en Python-systeempakketten, maakt de vaultstructuur aan en corrigeert eigenaar- en gebruikersrechten.

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

- [ ] `db/events.db` is geïnitialiseerd en schrijfbaar voor de servicegebruiker.
- [ ] Plugins zijn geregistreerd en event-routes zijn actief.
- [ ] De volledige venv staat onder `/home/infra/dev/venv`.
- [ ] `.env` bevat uitsluitend noodzakelijke configuratie en heeft beperkte rechten.
- [ ] `config/linkedin_auth.json` heeft mode `0600` wanneer live LinkedIn actief is.
- [ ] Substack/Medium-sessies zijn via CDP gekoppeld wanneer die kanalen actief zijn.
- [ ] `vault/`, `db/` en logmappen zijn schrijfbaar voor `infra`.
- [ ] Mocktests slagen vóór live publicatie wordt ingeschakeld.
- [ ] Dead-letter queue en screenshots worden operationeel gemonitord.

## Lokaal dashboard

De web-UI bindt standaard uitsluitend aan `127.0.0.1`:

```bash
./venv/bin/pip install -r dashboard/requirements.txt
./venv/bin/python3 dashboard/run.py --db db/events.db
```

Open daarna `http://127.0.0.1:8080`. Gebruik `--host 0.0.0.0` alleen achter
een vertrouwde firewall of reverse proxy met authenticatie.
