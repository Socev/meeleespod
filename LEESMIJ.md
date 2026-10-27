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
- **Bijlagen** (`bijlagen.py`, ontwerp § 5.3): alleen bij een uitdrukkelijk oordeel zakelijk/twijfel uit een
  live-ronde, alleen echte tekstlaag (pdf ≥ 200 tekens/pagina, geen beeld met kortste zijde ≥ 300 px of ≥ 250.000 px,
  geen ingesloten bestanden; docx/xlsx zonder plaatje > 50 kB); scans en foto's nooit. Staat **uit**
  (`MEELEES_BIJLAGEN=uit`) tot de n8n-tak `/outlook-bijlage` er is (B3).

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
ontwikkelaarsgereedschap, chrome:// en file:// geblokkeerd, popups buiten de inloghosts dicht. Na elke vangst en elke
Parro-ronde worden IndexedDB, Cache Storage, service workers en sessieopslag uit het profiel gewist (Teams-web en OWA
bewaren daar berichten); cookies en Local Storage (de MSAL-sessie) blijven.

Waarom eerst stil (review 27-10, MOET 1): Origin en Host bewijzen alleen iets tegenover een browser. Een proces in het
clusternetwerk dat de pagina-poort rechtstreeks bereikt, kan ze zelf invullen. Of dat kan (envoy-sidecar van Olares
vóór de poort?) is in B1 niet vast te stellen; daarom kan zo'n proces hooguit een Microsoft-inlogpagina openen, nooit
een ingelogd postvak. Meten in B2 vanuit de claudebot-pod.

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
- `/staat` (app-data): waterlijnen, gezien-lijsten (ids + tijd), oordeel per mail-id (alleen klasse), webhooksleutels,
  stand. Geen tekst, geen bijlagen.
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
