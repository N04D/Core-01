# UI/UX Self-Audit — Dashboard

Datum: 27 augustus 2026  
Scope: `dashboard/app.py`, `dashboard/authenticate.py`, `dashboard/run.py`, `dashboard/requirements.txt`, `dashboard/templates/index.html` en `dashboard/test_agenda.py`.

## Managementsamenvatting

Het dashboard heeft een coherente donkere visuele basis, consistente panelen en een bruikbare functionele dekking voor editor, drafts, agenda, media, publicaties en plugins. De grootste risico's liggen niet in de kleur- of kaartstijl, maar in de interactielaag: de interface is desktop-first, toont vrijwel nergens expliciete laadstatussen, is maar beperkt toegankelijk en gedraagt zich als een single-page-app zonder URL-, history- of focusbeheer.

De hoogste prioriteit is daarom: (1) robuuste mobiele navigatie, (2) een uniforme async action state met blijvende foutafhandeling, (3) toegankelijke namen/focus/live-regio's en (4) bescherming tegen verlies of dubbele uitvoering van gebruikerswerk. Pas daarna leveren verdere visuele verfijningen de meeste waarde.

## Positieve basis

- De visuele taal is grotendeels consistent: donker canvas, `panel`-kaarten, violet als primaire actie en semantische rood/groen/amber-statuskleuren.
- Bestands- en mediapaden worden server-side gevalideerd; uploads worden op extensie én magic bytes gecontroleerd.
- De agenda ondersteunt dag/week, kanalen, statussen, contenttypen en media-thumbnails.
- De uploadzone heeft al toetsenbordhandlers en de editor heeft een expliciet `aria-label`.
- Dynamische HTML-waarden worden in de meeste renders met `esc()` afgehandeld.
- Server-side acties leveren doorgaans bruikbare Nederlandse foutmeldingen terug.

## High impact

### H1 — Mobiele navigatie en viewport zijn niet bruikbaar genoeg

**Observatie**  
De root gebruikt een vaste `h-screen`/`overflow-hidden` layout en de zijbalk blijft op alle breakpoints `w-72`. Er is alleen een handmatige collapsed-state van 76 px; er bestaat geen mobiele drawer, overlay, sluitactie of responsive breakpoint. Daardoor houdt de navigatie op kleine schermen een groot deel van de breedte bezet. De driedelige agenda stapelt pas onder `xl`, maar blijft dan onder een permanente zijbalk staan. De weekkalender gebruikt bewust `min-w-[64rem]`, waardoor op tablet en mobiel een tweede horizontale navigatielaag ontstaat.

**Gevolg**  
Editor, Agenda en Media Store worden op telefoon of smalle tablet moeilijk of niet efficiënt bedienbaar. Mobiele browserbalken kunnen bovendien problemen geven met `h-screen`.

**Advies**

1. Maak de zijbalk onder `lg` een off-canvas drawer met backdrop, Escape-afhandeling, focus trap en een zichtbare hamburger in de hoofdheader.
2. Gebruik `min-h-dvh`/`h-dvh` met een fallback en laat de hoofdpagina verticaal scrollen.
3. Geef de agenda op mobiel standaard een dagweergave; bied week alleen als expliciete horizontale weergave aan.
4. Maak actiepanelen op mobiel sticky-bottom of een drawer, zodat draft, kalender en planning niet ver uit elkaar vallen.

**Acceptatiecriterium**  
Alle hoofdtaken zijn zonder horizontaal pagina-scrollen uitvoerbaar op 360 px, 768 px en 1280 px; alleen het weekraster zelf mag gecontroleerd horizontaal scrollen.

### H2 — Geen uniforme loading-, busy- en retry-states

**Observatie**  
`refreshCurrent()` wacht op requests maar toont geen skeleton, spinner of `aria-busy`. De meeste knoppen blijven actief tijdens requests. `createSchedule`, `saveEditor`, `dispatchDraft`, `cancelSchedule`, plugin toggles en media-linkacties kunnen daardoor dubbel worden uitgevoerd. `Promise.all()` in Home, Agenda en Publications maakt een complete view onbruikbaar wanneer één secundaire endpoint faalt. Alleen de upload en authenticatie tonen gedeeltelijke voortgang.

**Gevolg**  
Op een Raspberry Pi of bij grotere vaults lijkt de interface stil te staan. Dubbelklikken kan dubbele events of planningen veroorzaken. Een probleem met Telegram-status kan bijvoorbeeld de hele Agenda-load laten falen.

**Advies**

1. Introduceer één action-helper die submitters uitschakelt, een spinner/tekst toont en de oorspronkelijke state in `finally` herstelt.
2. Toon per widget een skeleton en lokale error-state met “Opnieuw proberen”; gebruik `Promise.allSettled()` waar secties onafhankelijk zijn.
3. Voeg lege, loading-, success- en error-state als vaste componentpatronen toe.
4. Maak muterende endpoints idempotent waar een dubbele publicatie schadelijk kan zijn.

**Acceptatiecriterium**  
Elke netwerkactie geeft binnen 100 ms zichtbare feedback, kan niet dubbel worden ingestuurd en biedt bij herstelbare fouten een lokale retry.

### H3 — Feedback is vluchtig en niet toegankelijk

**Observatie**  
De centrale flashmelding verdwijnt altijd na vier seconden, heeft geen `role="status"`, `aria-live` of focusmanagement en staat bovenaan de content. Een gebruiker die diep in Agenda of Media Store zit kan de melding missen. Meerdere gelijktijdige acties overschrijven elkaar. Validatie wordt alleen als algemene flash getoond en niet bij het bijbehorende veld.

**Gevolg**  
Screenreadergebruikers horen mutaties niet; gebruikers missen fouten of weten niet welk veld gecorrigeerd moet worden.

**Advies**

1. Gebruik een toast-stack met `role="status"` voor succes en `role="alert"` voor fouten; fouten blijven staan tot sluiten.
2. Toon veldvalidatie inline met `aria-describedby` en zet focus op het eerste foutieve veld.
3. Houd een klein activiteitenlog bij voor lange processen zoals uploads en authenticatie.

**Acceptatiecriterium**  
Alle mutaties worden zowel visueel als door een screenreader aangekondigd; kritieke fouten verdwijnen niet automatisch.

### H4 — Risico op werkverlies en onbedoelde mutaties

**Observatie**  
De editor heeft geen dirty-state, autosave, conceptherstel of waarschuwing bij bestand/view wisselen. “Nieuw” maakt alleen client-state aan; dat onderscheid is niet zichtbaar. Annuleren van een planning en uitschakelen van een plugin gebeuren onmiddellijk, zonder bevestiging of undo. Agenda-kanalen worden bij iedere load opnieuw standaard aangevinkt op basis van positie (`index < 3`), niet op gebruikersvoorkeur.

**Gevolg**  
Tekst kan ongemerkt verloren gaan en destructieve acties zijn foutgevoelig. De gekozen syndicatiekanalen kunnen afwijken van wat de gebruiker verwacht.

**Advies**

1. Voeg dirty-indicator, `beforeunload`-waarschuwing en bevestiging bij view-/bestandswisseling toe; overweeg lokale recovery.
2. Label nieuwe, nog niet opgeslagen bestanden expliciet.
3. Gebruik confirm/undo voor planning annuleren en plugin uitschakelen.
4. Bewaar kanaalkeuze als gebruikersinstelling en toon vóór submit een samenvatting.

**Acceptatiecriterium**  
Geen onopgeslagen editorinhoud kan zonder expliciete waarschuwing verdwijnen; risicovolle mutaties zijn bevestigbaar of herstelbaar.

### H5 — Toegankelijkheidsfundament is onvolledig

**Observatie**  
Veel inputs en selects hebben uitsluitend placeholders of nabije tekst, maar geen gekoppeld `<label>` of `aria-label`: Media Store-filters, editorselecties/bestandsnaam, agendafilters, draft search en media target. Icon-only controls zijn inconsistent gelabeld. Bij ingeklapte navigatie verdwijnen de tekstlabels met `display:none`, waarna symbolen zoals `⌂`, `▤` en `◈` de toegankelijke naam worden. Togglebuttons missen `aria-pressed`; de sidebar-knop mist `aria-expanded`/`aria-controls`. Dynamisch gekozen cards en calendar items hebben geen duidelijk focus- of selectie-attribuut. Status wordt vaak alleen met kleur aangegeven.

**Gevolg**  
Screenreader- en toetsenbordgebruikers kunnen controls niet betrouwbaar identificeren of de actuele state begrijpen.

**Advies**

1. Geef ieder form control een zichtbaar label of een expliciete accessible name.
2. Houd nav-tekst screenreader-only bij collapsed state; voeg tooltips en `aria-current="page"` toe.
3. Gebruik `aria-pressed` voor Dag/Week en `aria-selected` voor geselecteerde media/drafts.
4. Voeg consistente `focus-visible`-ringen toe en test volledige bediening zonder muis.
5. Combineer statuskleur altijd met tekst én een semantisch icoon.

**Acceptatiecriterium**  
Een axe-core scan bevat geen critical/serious violations en alle primaire flows zijn uitsluitend met toetsenbord uitvoerbaar.

## Medium impact

### M1 — Navigatie heeft geen URL of browsergeschiedenis

Alle views worden via `display:none` en lokale state gewisseld. Refresh, bookmarks, Back/Forward en deep links naar pluginsettings of Agenda werken niet. Implementeer echte routes of minimaal hash-routing met `history.pushState`, stateherstel en focus op de nieuwe paginatitel.

### M2 — Informatiearchitectuur dupliceert Drafts-functionaliteit

Er bestaan een losse Drafts-view, een conceptlijst in Agenda en bestandsselectie in de Editor. Ze hebben verschillende kanaalopties en acties (“Naar workflow” versus “Schedule”), waardoor niet duidelijk is welke plek leidend is. Kies één Draft Manager als primaire hub en laat Editor/Agenda daar contextueel naar verwijzen.

### M3 — Visuele hiërarchie en taal zijn inconsistent

- Home start met `text-3xl`, overige views met `text-2xl`; de sticky header herhaalt daarnaast de paginatitel.
- Nederlands en Engels lopen door elkaar: “Drafts”, “Publications”, “Schedule”, “Connected”, “Auth Required”, “Active/Disabled”, “Config”.
- Knoppen met dezelfde visuele nadruk hebben verschillende gevolgen: opslaan, plannen, dispatch en authenticeren delen dezelfde accentkleur.
- Zeer kleine tekst (`text-[10px]`) wordt gebruikt voor essentiële kalenderstatus en timestamps.

Maak een eenvoudige typografische schaal, woordenlijst en button hierarchy (primary/secondary/danger/quiet). Houd essentiële metadata minimaal 12–14 px.

### M4 — Agenda is visueel, maar nog geen complete planner

De kalender toont dagen als gelijke kolommen en items op volgorde, maar niet op een echte tijdas. Botsende tijden, timezone, huidige-tijdindicator en aantallen buiten de zichtbare periode ontbreken. Drafts/publicaties worden op filesystem-modificatiedatum in dezelfde kalender gezet, wat semantisch verwarrend kan zijn. Toon schedules als primaire kalenderlaag; plaats drafts in de drawer en publicaties/errors in een afzonderlijke activitylaag of maak deze lagen expliciet schakelbaar.

### M5 — Media-interactie kent onduidelijke selectie en drag/drop

De volledige dropzone heeft `role="button"` en toetsenbordactivatie, maar een gewone muisklik op de lege zone opent de filepicker niet; alleen “Bladeren” doet dat. Drag-state kan flikkeren door child `dragleave` events. De geselecteerde asset is hoofdzakelijk via randkleur herkenbaar en het target moet los gekozen worden voordat “Koppelen” werkt. Maak de hele zone klikbaar, gebruik een drag-counter, toon een duidelijke geselecteerde asset-toolbar en voeg toegankelijke selectie-state toe.

### M6 — Editor is afhankelijk van externe CDN's

Tailwind runtime en EasyMDE worden vanaf CDN geladen. Zonder internet is de UI ongestyled en ontbreekt de professionele editor, terwijl het product lokaal/soeverein hoort te werken. De fallback toont slechts een foutmelding. Bundle versie-gepinde CSS/JS lokaal, voeg integritybeleid toe voor externe fallback en test offline startup. De toolbar bevat bovendien nog `side-by-side`, hoewel de gewenste kernervaring één kolom is; verwijder of verberg die actie als één kolom productbeleid blijft.

### M7 — Auth-flow mist zichtbaarheid en beheer

De backend start een detached headed browser met alle output naar `/dev/null`. De UI pollt tot vijf minuten, maar heeft geen cancelknop, elapsed time, concrete foutreden of mogelijkheid om een verweesd proces te herkennen. Toon fasen (browser gestart, login verwacht, sessie valideren), bied annuleren/herstarten en surface een veilig geschoond foutresultaat.

### M8 — Filter- en viewstate gaan verloren

Agenda- en Media Store-filters, kalenderdatum, dag/weekmodus, geselecteerde draft/media en sidebarstate worden niet persistent opgeslagen. `loadAgenda()` bouwt kanaalopties opnieuw op en kan keuzes resetten. Bewaar niet-gevoelige voorkeuren in URL/localStorage en behoud selecties na refresh en mutaties.

### M9 — Datadichtheid en schaalbaarheid

Veel lijsten renderen alle ontvangen data direct via grote `innerHTML` strings. De planning-feed bevat tot honderden schedules/errors plus filesystem-items; Media Store tot 250 assets. Er is geen paginering, virtualisatie of progressive loading. Dit vergroot renderkosten op de Pi en maakt lange pagina's onrustig. Voeg server-side pagination/range queries, zichtbare result counts en lazy rendering toe.

## Low impact

### L1 — Ontbrekende microcopy en lege-state acties

Lege states vertellen meestal alleen dat er niets is. Voeg contextuele acties toe, zoals “Nieuw concept”, “Media uploaden”, “Plugin activeren” of “Ga naar huidige week”.

### L2 — Iconografie is niet gestandaardiseerd

Unicode-symbolen en emoji verschillen per platform en OS in vorm en alignment. Gebruik één lokale SVG-iconset met tekstalternatieven; behoud platformemoji alleen als decoratieve identiteit.

### L3 — Kalenderkaartjes trunceren belangrijke informatie

Titels worden afgekapt zonder toegankelijke volledige naam of detailweergave. Voeg tooltip/detailpopover toe en maak een kalenderkaart selecteerbaar voor inspectie/bewerking.

### L4 — Geen expliciete reduced-motion/contraststrategie

Er zijn transities en smooth scrolling zonder `prefers-reduced-motion`; subtiele `text-slate-600` op donkere achtergronden is waarschijnlijk te laag in contrast voor kleine tekst. Voeg reduced-motion overrides toe en valideer kleuren tegen WCAG AA.

### L5 — Productiestart gebruikt Flask development server

`run.py` gebruikt `app.run()`. Dit is primair operationeel, maar beïnvloedt UX door minder voorspelbaar gedrag onder gelijktijdige requests. Gebruik voor productie een WSGI-server en toon een duidelijke offline/degraded status bij backendproblemen.

## Testdekking en ontbrekende controles

De huidige dashboardtest controleert drie agenda-API/templatevoorwaarden. Er zijn geen browsertests voor echte interacties, responsive gedrag, toegankelijkheid of mislukte requests.

Aanbevolen minimale suite:

1. Playwright smoke-tests voor navigatie, editor dirty/save, draft scheduling, filteren, upload/select/link en plugin toggle/auth-state.
2. Viewporttests op 360×800, 768×1024 en 1440×900 met screenshotvergelijking.
3. axe-core op iedere view en een handmatige toetsenbordtest.
4. Tests met trage en falende endpoints, dubbele klik en offline CDN.
5. Uploadtests voor te groot, verkeerde magic bytes, gedeeltelijke failure bij meerdere files en keyboard/mouse/drop.
6. Locale/timezone-tests rond zomertijd en weekgrenzen.

## Aanbevolen uitvoeringsvolgorde

1. **UX-betrouwbaarheid:** async action states, lokale errors/retry, toast/live-regio en bescherming tegen dubbele acties.
2. **Responsive shell:** mobiele drawer, dynamische viewport, mobiele dagkalender en compacte actiepanelen.
3. **Accessibility pass:** labels, focus, ARIA states, kleurcontrast en keyboardflows.
4. **Werkbehoud:** editor dirty-state/recovery en confirm/undo voor risicovolle mutaties.
5. **Informatiearchitectuur:** Draft Manager als primaire flow, URL-routing en persistent filterstate.
6. **Performance/offline:** lokale frontend-assets, paginering en lazy rendering.
7. **Polish:** taal, iconen, typografie, microcopy en detailpopovers.

## Beslispunt

Er zijn tijdens deze audit geen dashboardbestanden gewijzigd. Implementatie hoort pas te starten na akkoord op de prioriteiten hierboven. De aanbevolen eerste implementatiefase bestaat uit H1–H3, gevolgd door H4–H5.
