# Systeem Audit Rapport

Audit uitgevoerd op 26 augustus 2026 vanuit de huidige projectwerkmap.

## Systeem Status

Het fundament is gedeeltelijk gebouwd. De drie belangrijkste Python-componenten zijn aanwezig en uitvoerbaar, maar het injectiescript, de database en het grootste deel van de vereiste vault-structuur ontbreken.

| Onderdeel | Status | Uitvoerbaar | Rechten / eigenaar | Opmerking |
|---|---|---:|---|---|
| `daemon/worker.py` | Aanwezig | Ja | `-rwxrwxr-x`, `infra:infra` | Worker-daemon aanwezig. |
| `core/setup_database.py` | Aanwezig | Ja | `-rwxrwxr-x`, `infra:infra` | Database-initialisatiescript aanwezig. |
| `plugins/channels/pub_mock.py` | Aanwezig | Ja | `-rwxrwxr-x`, `infra:infra` | Mock publisher aanwezig en registreerbaar. |
| `scripts/inject_mock_event.py` | **Ontbreekt** | Nee | Niet van toepassing | Vereist injectiescript moet nog worden gebouwd. |
| `db/events.db` | **Ontbreekt** | Niet van toepassing | Niet van toepassing | Database is nog niet geïnitialiseerd. |

### Vault-structuur

Tijdens de scan ontbraken de volgende vereiste mappen:

- `vault/skills`
- `vault/concepten`
- `vault/uitgaand`
- `vault/gepubliceerd`
- `vault/logs`

`vault/research` ontbrak eveneens tijdens de scan en is uitsluitend aangemaakt om dit auditrapport op de voorgeschreven locatie op te slaan. De vault-structuur is daardoor nog niet compleet.

### Integratiebevinding

De huidige worker start plugins met de JSON-payload via standaardinvoer, zonder `--event_id` en `--db`. De mock publisher vereist bij eventverwerking juist `--event_id` en leest de payload daarna uit SQLite. Zonder aanpassing van één van beide interfaces kan `pub_mock.py` niet succesvol door `daemon/worker.py` worden uitgevoerd.

## Database Status

`db/events.db` is niet gevonden. Daardoor konden de SQLite-integriteitscontrole, tabelcontrole en registratiecontrole niet worden uitgevoerd. De verwachte tabellen zijn pas beschikbaar nadat `core/setup_database.py` succesvol is uitgevoerd.

Let op: de standaardwaarde `../db/events.db` in `core/setup_database.py` wordt ten opzichte van de actieve shellwerkmap opgelost. Wanneer het script vanuit de projectroot wordt gestart, wijst die standaardwaarde buiten de projectmap. Start het script vanuit `core/`, geef expliciet `--database db/events.db` vanuit de projectroot op, of maak de standaardpadresolutie script-relatief.

## Actiepunten (TODO)

- [ ] Maak de ontbrekende vault-mappen aan: `skills`, `concepten`, `uitgaand`, `gepubliceerd` en `logs`.
- [ ] Initialiseer `db/events.db` en controleer daarna de aanwezigheid van `events_queue`, `dead_letter_queue`, `event_routes` en `plugin_registry`.
- [ ] Maak `scripts/inject_mock_event.py` aan en geef het uitvoerrechten met `chmod +x`.
- [ ] Installeer `python-dotenv`; de import `from dotenv import load_dotenv` in de worker kan momenteel niet worden geladen. Pakketnaam voor pip: `python-dotenv`.
- [ ] `watchdog` is niet geïnstalleerd. De huidige aangetroffen scripts importeren dit pakket niet, dus het is nu optioneel; installeer het alleen wanneer filesystem-events onderdeel van de daemon worden.
- [ ] Breng het uitvoercontract tussen de worker en `pub_mock.py` op één lijn (`--event_id`/`--db` doorgeven of een gedeeld stdin-protocol gebruiken).
- [ ] Registreer de mock publisher in `plugin_registry` nadat de database is aangemaakt.
- [ ] Voeg een route in `event_routes` toe die het gewenste event-type koppelt aan `Mock Publisher (Test Zandbak)`.
- [x] Geen aanvullende `chmod` nodig voor de drie aanwezige Python-scripts; alle drie zijn uitvoerbaar en eigendom van de huidige gebruiker.

## Eindoordeel

De uitvoerbare kernscripts zijn aanwezig, maar het systeem is nog niet end-to-end inzetbaar. De blokkerende punten zijn de ontbrekende database, het ontbrekende injectiescript, de onvolledige vault, de ontbrekende `python-dotenv`-dependency en het incompatibele worker/plugin-aanroepcontract.
