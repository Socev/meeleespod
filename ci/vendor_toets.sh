#!/usr/bin/env bash
# Toetst dat vendor/ precies de nagelezen code is (wv1435): elk bestand gelijk aan upstream op de vaste commit,
# behalve onze eigen SharePoint-stub. Ander commit of andere inhoud = stop (nieuwe nalezing nodig).
set -euo pipefail
cd "$(dirname "$0")/.."
T=$(mktemp -d)
haal() { git -c advice.detachedHead=false clone -q --filter=blob:none "https://github.com/weirdapps/$1.git" "$T/$1"; git -C "$T/$1" checkout -q "$2"; [ "$(git -C "$T/$1" rev-parse HEAD)" = "$2" ]; }
haal outlook-access cb4d299eb3c608ef7cb9c324cdd1f4392ebb83d5
haal teams-access 1aa084d4fd840ef281917afe401c796430988eb2
fout=0
while IFS= read -r f; do
  rel=${f#vendor/}; pakket=${rel%%/*}; pad=${rel#*/}
  if [ "$pad" = "src/auth/sharepoint-capture.ts" ] && [ "$pakket" = "outlook-access" ]; then
    grep -q "EIGEN VERVANGING (meeleespod)" "$f" || { echo "::error::stub zonder merkteken: $f"; fout=1; }
    continue
  fi
  if cmp -s "$f" "$T/$pakket/$pad"; then echo "  OK    $rel"; else echo "::error::afwijkend van upstream: $rel"; fout=1; fi
done < <(find vendor -type f ! -name BRON.md | sort)
# De schrijvende bestanden mogen niet in vendor/ staan.
for x in send-mail reply forward move-mail create-folder delete-draft capture-signature download-sharepoint-link \
         sharepoint-client send-message lookup-users cli.ts outlook-client graph-client chatsvc-client chatsvcagg-client; do
  if find vendor -name "*${x}*" | grep -q .; then echo "::error::schrijvend/ongewenst bestand in vendor: $x"; fout=1; fi
done
rm -rf "$T"
exit $fout
