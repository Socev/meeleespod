#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# pak.sh [chart] — maakt <chart>-<versie>.tgz voor de Olares Market (Upload custom chart); alleen meeleespod.
# Geen enkele chart mag een map van een ANDERE app mounten (les health-pod 11-10-2026: verwijderen wiste de data).
CHART="${1:-meeleespod}"
case "$CHART" in meeleespod) ;; *) echo "FOUT: alleen meeleespod" >&2; exit 1;; esac
# Poort (wv430): een hostPath buiten de eigen appData (dir .Values.userspace.appData, of een vaste /olares/...-weg
# naar Data/<andere app>) weigert het inpakken. De eigen appData ({{ .Values.userspace.appData }}/...) mag wel.
if grep -rnE 'dir[[:space:]]+\.Values\.userspace\.appData|/Data/[a-z0-9-]+/|\.\./' "$CHART/templates" >&2; then
  echo "FOUT: $CHART mount (vermoedelijk) een map van een andere app (regels hierboven). Dat kan bij het verwijderen" >&2
  echo "      van die andere app de data van deze app wissen (11-10-2026). Niet inpakken." >&2
  exit 1
fi
#
# Overgenomen uit claudebot-chart (3-10-2026), zelfde poort:
# POORT (22-9-2026). Vóór 22-9 pakte dit script blind in. Dat ging een keer mis: de
# bump naar 0.0.48 stond wel lokaal gecommit maar niet op GitHub, en niemand zag dat
# aan de .tgz. Een chart die je uploadt terwijl de repo iets anders zegt, is niet meer
# te reconstrueren. Daarom weigert dit script nu in te pakken als de werkboom vuil is
# of als lokaal achterloopt op origin; ongepushte commits duwt hij er eerst zelf naartoe.

[ -f "$CHART/Chart.yaml" ] || { echo "FOUT: geen $CHART/Chart.yaml naast dit script." >&2; exit 1; }

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "FOUT: dit is geen git-repo. De chart hoort in Socev/meeleespod (map chart/) te staan," >&2
  echo "      anders is niet te achterhalen wat er is geüpload." >&2
  exit 1
fi

echo "controle: origin ophalen..."
if ! git fetch origin --quiet; then
  echo "FOUT: 'git fetch origin' mislukte. Zonder contact met GitHub kan ik niet vaststellen" >&2
  echo "      of deze chart gelijkloopt met de repo. Controleer netwerk of credential." >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "FOUT: de werkboom is niet schoon. Commit of herstel eerst deze wijzigingen:" >&2
  git status --short >&2
  exit 1
fi

tak="$(git rev-parse --abbrev-ref HEAD)"
achter="$(git rev-list --count "HEAD..origin/${tak}" 2>/dev/null || echo 0)"
voor="$(git rev-list --count "origin/${tak}..HEAD" 2>/dev/null || echo 0)"

if [ "$achter" -gt 0 ]; then
  echo "FOUT: lokaal loopt $achter commit(s) ACHTER op origin/${tak}." >&2
  echo "      Je zou een oudere chart inpakken dan wat er in de repo staat." >&2
  echo "      Haal eerst op:  git pull --ff-only" >&2
  exit 1
fi

if [ "$voor" -gt 0 ]; then
  echo "let op: $voor commit(s) nog niet gepusht — ik push ze nu naar origin/${tak}."
  if ! git push origin "$tak"; then
    echo "FOUT: pushen mislukte. De chart wordt NIET ingepakt: een .tgz die niet" >&2
    echo "      terug te vinden is in de repo, mag niet naar Olares." >&2
    exit 1
  fi
  echo "gepusht."
fi

versie="$(sed -n 's/^version:[[:space:]]*//p' "$CHART/Chart.yaml" | head -1 | tr -d '"')"
[ -n "$versie" ] || { echo "FOUT: geen version-regel in $CHART/Chart.yaml." >&2; exit 1; }

olares-cli chart lint "$CHART" || { echo "FOUT: chart lint faalt; niet inpakken." >&2; exit 1; }
uit="${CHART}-${versie}.tgz"
rm -f "$uit"
tar -czf "$uit" "$CHART"
echo "klaar: $uit  (commit $(git rev-parse --short HEAD), gelijk aan origin/${tak})"
tar -tzf "$uit" | head -3
