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

Storage-state bevat sessiegeheimen. Deel of commit dit bestand niet. Bij een verlopen sessie moet het opnieuw worden gegenereerd.

## systemd

Units:

- `deploy/social-worker.service`
- `deploy/social-watchdog.service`

Installeren:

```bash
sudo ./deploy/install_services.sh
```

Het installatiescript kopieert de units naar `/etc/systemd/system/`, voert `systemctl daemon-reload` uit en gebruikt `systemctl enable --now` voor beide services.

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
- [ ] `vault/`, `db/` en logmappen zijn schrijfbaar voor `infra`.
- [ ] Mocktests slagen vóór live publicatie wordt ingeschakeld.
- [ ] Dead-letter queue en screenshots worden operationeel gemonitord.
