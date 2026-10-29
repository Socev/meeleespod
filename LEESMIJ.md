# meeleespod

Outlook en Teams van de praktijk en Parro **alleen lezen**, zonder de werk-pc. Eigen Olares-app (niet in de
claudebot-pod: het praktijktoken en de inlogcookies kunnen technisch ook versturen en horen niet waar Socev en alle
sleutels draaien). Ontwerp: vault `01_Ontwikkeling/Meeleespod - Outlook, Teams en Parro zonder de werk-pc (ontwerp
27-10-2026).md` (wv1435, Fable-review verwerkt); bouw B1: wv1710.

## Wat de pod doet
- **Lezers** (`meeleespod/outlook.py`, `teams.py`, `parro.py`): eigen dunne GET-clients; zelfde payload, velden, ids,
  blokgroottes en webhooks als de pc-scripts (`Socev/socev-pc` 30524ca), met `script_versie = pod-<commit>`.
  Het patiëntfilter blijft in n8n (Qwen): de pod stuurt wat de pc stuurde, naar dezelfde plek.
- **Vangst** (`vanger/`): alleen de inlogfunctie van de twee pakketten (zie `vendor/BRON.md`), zichtbaar op een virtueel
  scherm (Xvfb). Stil vernieuwen als een token < 20 min geldig is; lukt dat niet: alarm `[dienst/login]`.
- **Planner**: Outlook en Teams elk uur 07–23 en 03:00, Parro elke 2 uur 07–21; één browser tegelijk.
- **Modus per dienst**: Outlook en Teams beginnen in `proef` (dry_run: n8n classificeert, bewaart niets; ontwerp § 8.1),
  Parro begint `live` met **zaaien** (alles van vóór 25-10 15:17, de laatste pc-ronde, geldt als verstuurd; § 8.2).
  Een dienst draait pas na de eerste geslaagde inlog; daarvoor geen rondes en geen alarm. `proef` draait nooit vanzelf:
  één proefronde volgt op de inlog, daarna alleen via `/api/ronde`; `live` zet de machinekamer met `/api/modus`.
- **Bijlagen** (`bijlagen.py`, ontwerp § 5.3, B3b wv1751): alleen bij een uitdrukkelijk oordeel zakelijk/twijfel uit een
  live-ronde dat Qwen zelf gaf (n8n `terugval: true` = niet bewaard, nooit bijlagen); alleen echte tekstlaag; scans en
  foto's nooit. Grenzen gelijk aan n8n: ≤ 11 MiB, tekst ≤ 30.000 tekens (JavaScript-lengte; langer = niet versturen,
  niet afkappen). Zie **Bijlagenpad** hieronder. Aan sinds chart 0.0.4 (`MEELEES_BIJLAGEN=aan`).

## Bijlagenpad (B3b, wv1751)
Doel: geen bestand met patiëntgegevens laten doorgaan waarvan Qwen de tekst nooit zag.
- **docx/xlsx**: witte lijst van onderdelen (tekst-, vorm- of beelddeel); al het andere wordt geweigerd (`office_onbekend_deel`).
  Altijd weigeren: draaitabel (`xl/pivotCache`, `pivotTables`), externe koppeling, gegevensverbinding (`connections.xml`,
  `queryTables`, `model/`, slicers), altChunk, ingesloten objecten (`embeddings`, ActiveX), macro's (ook via het
  inhoudstype), `customXml/`, bouwstenen (`word/glossary`), dubbele zipnamen, versleutelde delen, en relaties van die
  soorten in een `.rels`. Beeld alleen png/jpeg/gif (magische bytes) ≤ 50 kB en kleiner dan 300 px kortste zijde /
  250.000 px; emf/wmf/svg nooit (die kunnen tekst dragen). Naar Qwen gaat de tekst van **alle** XML-delen (ook stijlen,
  thema, instellingen: welk deel Word toont, bepalen `_rels` en `[Content_Types]`, niet de naam), dus ook voetnoten,
  eindnoten, opmerkingen, verwijderde tekst, veldcodes, bladnamen, formules en documenteigenschappen, plus alle
  attribuutwaarden met mensentekst (alt-tekst, tooltip, watermerk, docVar, gegevensvalidatie; ontdubbeld, opmaak weg).
- **pdf**: gemeten (poppler 22.12, verzonnen pdf): `pdftotext` neemt formulierwaarden en FreeText mee, maar niet de tekst van
  een notitie (`/Text`) of andere opmerking. Daarom weigeren bij een annotatie anders dan `/Link`, een formulier (velden,
  `/FT`, XFA; een leeg `/AcroForm` mag — gemeten: komt veel voor), ingesloten bestanden, versleuteling, een indirecte
  `/Subtype`, of een objectstroom die niet met Flate uit te pakken is. Twee lagen: **pypdf 6.21.3** (strikt; per object
  soepel als strikt struikelt; elk xref-object nagelopen) en een ruwe scan van bytes + objectstromen (NUL/commentaar als
  witruimte, `#xx`-namen). Titel, onderwerp, trefwoorden, auteur, bladwijzers, XMP en linkomschrijvingen gaan als extra
  tekst mee. Scanbeelden, ingesloten bestanden en minimale tekst per pagina zoals voorheen.
- **Afleveren**: `POST /outlook-bijlage` met time-out 960 s (n8n: Qwen tot 3 × 240 s + Jev + Intake), `contact=False`
  (de contacttijd hoort bij de classificatie). Alleen `ok: true` + `door: true` telt als doorgelaten; `door: false`
  (200) en andere 4xx zijn een definitieve weigering.
- **Herkansing**: bij n8n 503 (Qwen), een andere 5xx (Intake legde niet vast), time-out/verbindingsfout of Outlook 5xx gaat
  alleen `mail_id`, `thread_id`, `v2id`, `bijlage_id`, `pogingen`, `sinds` naar `/staat/bijlage_wachtrij.json` (≤ 50,
  geen naam, geen inhoud). Eerstvolgende live-ronde: eerst de wachtrij (≤ 10 per ronde), precies 3 herkansingen, dan
  `bijlage_opgegeven` in het log. Een storing die elke bijlage raakt (n8n 401/403 = sleutel, geen sleutel, Outlook
  401/429) pauzeert de rest van de ronde zonder netwerk en telt niet als poging. Opnieuw sturen is veilig: de Intake geeft
  bij een herhaling `dubbel` en n8n antwoordt dan `door: true`.
- **Stand**: `laatste_ronde` van Outlook toont `doorgelaten`, `geweigerd`, `herkansing`, `opgegeven`, `wachtrij` en
  `terugval` (alleen aantallen); weigerredenen staan in het log (`bijlage_geweigerd reden=…`).
- Bewust geweigerd en te meten na live: `customXml/` komt in gewone Word-/SharePoint-documenten voor (bibliografie,
  metadata) en kost dan een bijlage; tel `reden=office_customxml` in het log voordat je dit verruimt.

## Ingangen
| ingang | poort | authLevel | wat |
|---|---|---|---|
| meeleespod | 8080 | private (Olares-login + 2FA) | inlogpagina: stand + knop Inloggen; venster via noVNC |
| meeleesapi | 8081 | internal | API voor Socev: `GET /api/stand`, `POST /api/ronde`, `/api/koppel` (alleen sleutels), `/api/modus` |

Inloggen: een klik (eigen kop + Origin van de pagina) start EERST een stille vernieuwing zonder scherm. Lukt die, dan
komt er geen venster. Alleen als echt ingelogd moet worden, start x11vnc (alleen localhost, willekeurige poort, `-once`)
en krijgt alleen de aanvrager (aanvraagtoken) een eenmalige code (2 min geldig, één verbinding, in de URL-hash). In dat
venster staat dan de inlogpagina van Microsoft of Parro, geen postvak. Het sluit zodra het token gevangen is of na
10 min; elke start gaat als `[dienst/inlogvenster]`-regel naar n8n. Browserbeleid: kiosk, geen downloads, geen
ontwikkelaarsgereedschap (devtools:// geblokkeerd; het beleid DeveloperToolsAvailability kan niet: dat sluit ook het
besturingskanaal van Playwright), chrome:// en file:// geblokkeerd, popups buiten de inloghosts dicht. Na elke vangst en elke
Parro-ronde worden IndexedDB, Cache Storage, service workers en sessieopslag uit het profiel gewist (Teams-web en OWA
bewaren daar berichten); cookies en Local Storage (de MSAL-sessie) blijven. **Parro** (wv1751, gemeten 28-10-2026): de
Parro-webapp bewaart toegangs- en vernieuwtoken in de IndexedDB-databank `parro_db` (Drift/SQLite); die wissen gaf
"uitgelogd" zodra het toegangstoken na 1 u verliep. Voor Parro blijven IndexedDB, File System en WebStorage daarom staan
(Parro bevat geen patiëntgegevens, wel kindergegevens; `/geheim` zit niet in de NAS-back-up); caches gaan wel weg.
**Parro-sessiemeting** (wv1911, chart 0.0.5): Parro viel 28-10 ± 8 u na de inlog uit terwijl stil vernieuwen tussendoor
lukte. Elke ronde bewaart nu in `/staat/parro_sessie.json` (laatste 24) en in `/api/stand` (`diensten.parro.sessie`) alleen
namen, tijden, statussen en een hash van 8 hex per vernieuwtoken: welke POST de vernieuwing deed, `antwoord_tijden`
(bv. `refresh_expires_in`), `rt_tijden` (als het vernieuwtoken een JWT is), claimnamen en `auth_time` van het
toegangstoken, en `rotatie_bewaard` (stuurde deze ronde het vernieuwtoken dat de vorige ronde kreeg?). Na een geziene
vernieuwing wacht de ronde 10 s vóór het sluiten, zodat de app het nieuwe token in `parro_db` kan wegschrijven.

Waarom eerst stil (review 27-10, MOET 1): Origin en Host bewijzen alleen iets tegenover een browser. Een proces in het
clusternetwerk dat de pagina-poort bereikt, kan ze zelf invullen. Daarom kan zo'n proces hooguit een Microsoft-inlogpagina
openen, nooit een ingelogd postvak.

**Gemeten 27-10-2026 (B2, wv1716) vanuit de claudebot-pod:**
- Via het olares.com-adres van de inlogpagina (`9c3d81170.primumnonnocere.olares.com`, binnen Olares 192.168.20.54) komt
  een pod op Olares er **zonder Olares-login** bij (`/gezond` en `/stand.json` 200), en de knopkop + Origin zijn na te
  maken: `POST /inloggen?dienst=onbekend` met `X-Meelees-Knop: 1` en `Origin: https://9c3d81170…` gaf 400 (onbekende
  dienst) in plaats van 403. Een proces in het cluster kan dus de inlogknop drukken (en als aanvrager de VNC-code krijgen).
- Via de publieke tunnel (`--resolve` naar het publieke IP) gaven beide ingangen 302 naar de Olares-login.
- `meeleespod.meeleespod-primumnonnocere.svc.cluster.local:8080/8081` is vanuit claudebot dicht (linkerd 503 / reset).
- Gevolg: authLevel private beschermt alleen tegen het netwerk buiten Olares, niet tegen andere apps op Olares. De
  stil-eerst-regel blijft daarom nodig; een knop die een venster sluit is ook vanuit het cluster te drukken.

## Wat de pod verlaat (precies dit)
POST naar de vaste adressen in `meeleespod/instellingen.py` (outlook-classificatie, teams-classificatie,
parro-meelezen-q8t3n, en later outlook-bijlage). GET alleen naar outlook.office.com, teams.microsoft.com en
rest-v2.parro.com. Afgedwongen door het **vangnet** (`vangnet.py`): andere host, andere methode of
een redirect = weigering + alarm.

## Egress (gemeten 27-10-2026, wv1710)
- `olares-cli chart lint` accepteert een NetworkPolicy met egress-regels in de chart.
- Maar: Olares' app-service (`framework/app-service/controllers/security_controller.go`, `createOrUpdateNetworkPolicy`,
  beclab/Olares main 8943631) verwijdert bij elke reconcile van een app-namespace **elke NetworkPolicy die niet van
  Olares zelf is** (uitgezonderd alleen route-control/caller-JWT/app-gateway-mesh). Een eigen egress-regel blijft dus
  niet staan; de labels van Olares nabootsen is misbruik en breekt bij een update. Olares' eigen regels zijn alleen
  Ingress. CNI = Calico (zou het wel afdwingen), maar een FQDN-lijst kan standaard-NetworkPolicy niet aan.
- **Uitkomst: egress niet afgedwongen op netwerkniveau.** De grens is het vangnet in de eigen code; Chromium (moet bij
  inloggen naar alle Microsoft-/Parro-inloghosts) en een gekaapt proces vallen erbuiten. Niet op de echte Olares gemeten
  (niets installeren in B1); de geïnstalleerde Olares-versie kan afwijken van main.

## Restrisico's (bewust, benoemd)
- Chromium draait zonder sandbox (Playwright-standaard in een container zonder user namespaces) en toont Microsoft- en
  Parro-pagina's in een pod met tokens en webhooksleutels.
- Stil vernieuwen opent Teams-web/OWA kort op het virtuele scherm; of dat chats als gelezen markeert, is niet vast te
  stellen zonder inlog (meten in B4).
- Wie Davids Olares-login met 2FA heeft, kan in een open inlogvenster werken (zoals aan zijn ontgrendelde pc).

## Opslag
- `/staat` (app-data): waterlijnen, gezien-lijsten (ids + tijd), oordeel per mail-id (alleen klasse), bijlage-wachtrij
  (alleen ids), webhooksleutels, stand. Geen tekst, geen bijlagen.
- `/geheim` (app-cache, niet in de NAS-back-up): `lezer.json` per dienst en de browserprofielen (0700/0600).
- `/werk` (tmpfs): bijlage heel even voor pdftotext. `/tmp` (emptyDir): Xvfb, Chromium.
- Logs: witte lijst (`logboek.py`); van kind-stderr alleen `exit=`, `HTTP nnn`, `code=`.

## Toetsen
`python -m pytest tests` (lokaal/CI), `ci/vendor_toets.sh`, `ci/bundel_grep.sh`, `ci/rooktest.sh <image>` (alleen-lezen
rootfs, uid 1000, sleutel, koppel zonder adres, geen VNC zonder klik, zelftoets, beleidtoets, geen sleutels in het log).
Chart: `olares-cli chart lint chart/meeleespod`, inpakken met `chart/pak.sh` (alleen schone, gepushte werkboom).

## Installeren (David, B2)
Olares → Market → Upload custom chart (`meeleespod-<versie>.tgz`); daarna de pagina openen en per dienst één keer
inloggen. Machinekamer: adres van de API-ingang in `/opt/data/.meeleespod/adres`, webhooksleutels via
`socev/meelees.py koppel <bestand>`.

Sinds 27-10-2026 installeert en werkt de machinekamer zelf bij: `olares-eigen upload chart/meeleespod-<versie>.tgz`,
daarna `olares-eigen installeer meeleespod <versie>` (eerste keer) of `olares-eigen upgrade meeleespod <versie>`.
Adressen (gemeten 27-10): inlogpagina `https://9c3d81170.primumnonnocere.olares.com`, API
`https://9c3d81171.primumnonnocere.olares.com` (appid = md5("meeleespod")[:8] + volgnummer van de ingang).
Een upgrade (0.0.1 → 0.0.2) liet `/staat` (sleutels, stand) én `/geheim` (Outlook-sessie) staan.

Herstel na uitlog (sinds 0.0.4): `planner.hersteld()` wist de contacttijd van die dienst, zodat de eerstvolgende ronde
meteen een levensteken stuurt en n8n (*Pod-contact?* → *AI - Meeleespod-seintjes*) de login-rij dichtzet.
Versie: het image weigert te bouwen zonder volledige `GIT_SHA` (geen `pod-dev` meer); de rooktest controleert het.

Verbindingsfouten (sinds 0.0.2): `laatste_ronde.code` = `VERBIND_<DNS|CERT|TLS|GEWEIGERD|RESET|TIMEOUT|GEEN_ROUTE|OS<errno>|OVERIG>_<HOST>`,
in het log `verbind_fout host=… code=…`; geen fouttekst. Sinds 0.0.3 ook een niet-401 HTTP-fout als `HTTP<status>_<HOST>`
(bv. `HTTP500_OUTLOOK`), zelfde vorm, geen antwoordtekst.

## Teams via de MCAS-proxy (sinds 0.0.3, wv1736)
De tenant (Huisartsen Eemland) draait Defender for Cloud Apps. De eerste Teams-navigatie loopt via de MCAS-proxy en komt
op een "omleidingsmelding" (`mcas-proxyweb.mcas.ms/certificate-checker`). Het vendor-pakket ruimt die op door op
"Proceed to …" te klikken — maar op `Accept-Language: nl-NL` staat er "Doorgaan naar …" en matcht de selector niet, dus
bleef het inlogvenster hangen tot de time-out (gemeten 27-10-2026, B2). Daarom zet `vanger/src/vanger.ts` voor
**Teams** `locale = 'en-US'` (Accept-Language); dan rendert de proxy "Proceed to …" en klikt het pakket de melding zelf
weg. Gemeten vanuit de pod: zelfde IP, nl→NL, en→EN, dus de taal volgt Accept-Language; de Microsoft-inlogpagina is dan
Engels voor David (eenmalig, cosmetisch). Outlook logde al goed in op nl-NL en blijft nl. Geen MCAS-CA nodig: Chromium
in de pod vertrouwt `*.mcas.ms` al (geen cert-melding gemeten).

## Vastgelopen inlogvenster (sinds 0.0.3)
Een openstaand inlogvenster blokkeert de inlogknoppen. De **hoofdpagina** toont nu welk venster openstaat en een knop
**Venster sluiten** (`POST /inloggen/stop`), naast de bestaande knop in het VNC-venster. `draai_vanger` stopt een net
gestarte zichtbare vanger meteen als het venster in dat gaatje al gesloten blijkt (`code=GESTOPT`), zodat het
browser-slot niet 10 min vast blijft (review-bevinding wv1736).
