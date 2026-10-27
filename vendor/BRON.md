# Vendor: alleen de inlogvangst van twee pakketten

| pakket | bron | commit | nagelezen |
|---|---|---|---|
| outlook-access | github.com/weirdapps/outlook-access | cb4d299eb3c608ef7cb9c324cdd1f4392ebb83d5 (v1.5.0, 26-10-2026) | wv1435, 27-10-2026 |
| teams-access | github.com/weirdapps/teams-access | 1aa084d4fd840ef281917afe401c796430988eb2 (v0.1.0, 26-10-2026) | wv1435, 27-10-2026 |

Alleen de bestanden die de vangfunctie (`captureOutlookSession`, `captureSession`) nodig heeft, byte-gelijk aan upstream
(`ci/vendor_toets.sh` toetst dat in elke CI-run). Eén eigen vervanging: `outlook-access/src/auth/sharepoint-capture.ts`
(stub die altijd weigert; merkteken "EIGEN VERVANGING (meeleespod)").

Bewust NIET hier (schrijvend of onnodig): send-mail, reply, forward, move-mail, create-folder, delete-draft,
capture-signature, download-sharepoint-link, sharepoint-client, send-message, lookup-users, beide cli.ts, en de
leesclients (outlook-client, graph-client, chatsvc-client, chatsvcagg-client): de pod leest met een eigen dunne
GET-client (meeleespod/outlook.py en teams.py). Een update van een pakket = nieuwe nalezing + nieuwe commit hier.
Licentie: MIT (LICENSE per pakket, ook in het image).
