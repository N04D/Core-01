# Core-01 Writer

De Markdown Editor in het dashboard is een lokaal gebundelde Milkdown Crepe
Writer. De productiepagina laadt `dashboard/static/editor.bundle.js` en
`.css`; runtime-installaties hebben dus geen npm of internetverbinding nodig.

## Opslag en migratie

Markdown blijft de canonieke representatie. Nieuwe en gewijzigde bestanden
worden opgeslagen in:

| Gebied | Runtimepad |
|---|---|
| Concepten | `$CORE_DATA/concepts` |
| Uitgaand | `$CORE_DATA/outgoing` |

`vault/concepten` en `vault/uitgaand` zijn alleen legacy-bronnen. De
copy-only migratie is recursief, overschrijft niets en verwijdert niets:

```bash
CORE_DATA=/var/lib/core-01 python3 scripts/migrate_runtime_data.py --dry-run
CORE_DATA=/var/lib/core-01 python3 scripts/migrate_runtime_data.py
```

## Werking

Frontmatter wordt vóór het openen gesplitst en exact teruggeplaatst bij het
opslaan. Onbekende YAML-velden worden dus niet herschikt of verwijderd. De
Writer ondersteunt headings, lijsten, links, quotes, code, tabellen, slash- en
toolbar-controls, undo/redo en een later uitbreidbare `insertMarkdown`/
`insertImage`-hook voor de Media Store.

Wijzigingen krijgen na circa 1,2 seconden debounce een seriële PUT naar de
bestaande file-API. Een wijziging tijdens een lopende save blijft dirty en
wordt daarna opnieuw opgeslagen. `Ctrl+S`/`Cmd+S`, de handmatige Opslaan-knop
en de `beforeunload`-waarschuwing blijven beschikbaar. De status toont
Opgeslagen, Niet opgeslagen, Opslaan… of Opslaan mislukt, plus woordenaantal en
geschatte leestijd (225 woorden/minuut; frontmatter telt niet mee).

Bij een asset-loadfout blijft een plain textarea-fallback beschikbaar. De
Drafts-pagina leest dezelfde concepten en dispatcht nog steeds via de normale
eventbus; de Writer publiceert zelf nooit rechtstreeks naar kanalen.
