#!/usr/bin/env bash
# Rooktest van het image in de vorm van de chart: alleen-lezen rootfs, uid 1000, /tmp + /werk als tmpfs, nep-sleutels,
# GEEN inlog en GEEN echte webhook (de pod verstuurt in deze test niets: er is geen ingelogde dienst).
set -euo pipefail
IMAGE="$1"
SLEUTEL=nep-ci-sleutel-0123456789abcdef
H() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
M=$(mktemp -d); mkdir -p "$M/staat" "$M/geheim"; sudo chown -R 1000:1000 "$M"
docker run -d --name meelees-rook --read-only --tmpfs /tmp:rw,exec,size=512m --tmpfs /werk:rw,size=64m --shm-size 512m \
  -v "$M/staat:/staat" -v "$M/geheim:/geheim" -p 18080:8080 -p 18081:8081 \
  -e MEELEES_SOCEV_HASHES="$(H $SLEUTEL)" "$IMAGE" pod >/dev/null
for i in $(seq 1 40); do curl -fsS -m 2 http://127.0.0.1:18081/gezond >/dev/null 2>&1 && curl -fsS -m 2 http://127.0.0.1:18080/gezond >/dev/null 2>&1 && break; sleep 1; done
c() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
eis() { if [ "$2" = "$3" ]; then echo "  OK    $1 ($2)"; else echo "::error::$1: verwacht $3, kreeg $2"; docker logs meelees-rook | tail -20; exit 1; fi; }
A=http://127.0.0.1:18081; P=http://127.0.0.1:18080
eis "uid 1000" "$(docker exec meelees-rook id -u)" 1000
eis "rootfs alleen-lezen" "$(docker exec meelees-rook sh -c 'touch /app/x 2>/dev/null && echo schrijfbaar || echo alleen-lezen')" alleen-lezen
eis "API zonder sleutel" "$(c $A/api/stand)" 401
eis "API met sleutel" "$(c -H "Authorization: Bearer $SLEUTEL" $A/api/stand)" 200
eis "koppel met adres geweigerd" "$(c -X POST -H "Authorization: Bearer $SLEUTEL" -d '{"meelees":"nep-ci-webhook-0123456789","url":"https://elders.example"}' $A/api/koppel)" 400
eis "koppel alleen sleutel" "$(c -X POST -H "Authorization: Bearer $SLEUTEL" -d '{"meelees":"nep-ci-webhook-0123456789"}' $A/api/koppel)" 200
eis "API-poort heeft geen inlogknop" "$(c -X POST -H 'X-Meelees-Knop: 1' $A/inloggen?dienst=outlook)" 404
eis "pagina-poort heeft geen API" "$(c -H "Authorization: Bearer $SLEUTEL" $P/api/stand)" 404
eis "inlogpagina" "$(c $P/)" 200
eis "knop zonder kop geweigerd" "$(c -X POST $P/inloggen?dienst=outlook)" 403
eis "status van een onbekende aanvraag" "$(c -H 'X-Meelees-Aanvraag: verzonnen' "$P/inloggen/status?id=verzonnen")" 404
eis "knop van andere site geweigerd" "$(c -X POST -H 'X-Meelees-Knop: 1' -H 'Origin: https://kwaad.example' $P/inloggen?dienst=outlook)" 403
eis "VNC zonder code geweigerd" "$(c -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Sec-WebSocket-Version: 13' -H 'Origin: http://127.0.0.1:18080' "$P/vnc/ws?code=verzonnen")" 403  # gitleaks:allow (RFC 6455-voorbeeld)
eis "noVNC aanwezig" "$(c $P/novnc/core/rfb.js)" 200
eis "geen x11vnc zonder klik" "$(docker exec meelees-rook sh -c 'cat /proc/[0-9]*/comm 2>/dev/null | grep -c "^x11vnc$" || true')" 0
eis "geen sessie- of profielbestanden zonder inlog" "$(sudo find "$M/geheim" -type f | wc -l | tr -d ' ')" 0
docker exec meelees-rook python3 -m meeleespod zelftoets
docker exec meelees-rook python3 -m meeleespod beleidtoets
docker exec meelees-rook sh -c 'node /app/vanger/dist/vanger/src/vanger.js; echo "exit=$?"' | tee /tmp/vanger.uit
if grep -q '"code":"GEBRUIK"' /tmp/vanger.uit && grep -q '^exit=1$' /tmp/vanger.uit; then echo "  OK    vanger laadt (zonder opdracht: GEBRUIK)"; else echo "::error::vanger laadt niet goed"; exit 1; fi
docker exec meelees-rook node -e "require('/app/vanger/dist/vendor/outlook-access/src/auth/browser-capture.js'); require('/app/vanger/dist/vendor/teams-access/src/auth/browser-capture.js'); console.log('  OK    vangmodules laden (playwright gevonden)')"
D=$(mktemp -d); docker cp meelees-rook:/app/vanger/dist "$D/dist"; docker cp meelees-rook:/app/meeleespod "$D/py"
bash "$(dirname "$0")/bundel_grep.sh" "$D/dist" "$D/py"
for t in send-mail reply forward move-mail send-message lookup-users cli.js outlook-client graph-client chatsvc-client; do
  if find "$D/dist" -name "*$t*" | grep -q .; then echo "::error::$t staat in het image"; exit 1; fi
done; echo "  OK    geen schrijvende pakketbestanden in het image"
if docker logs meelees-rook 2>&1 | grep -E "$SLEUTEL|nep-ci-webhook|Bearer" ; then echo "::error::sleutel in het log"; exit 1; fi
echo "  OK    geen sleutels in het log"
eis "sleutels.json 0600" "$(sudo stat -c %a "$M/staat/sleutels.json")" 600
echo "rooktest: GESLAAGD"
