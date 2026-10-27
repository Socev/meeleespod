#!/usr/bin/env bash
# Ontwerp § 10 B1: de bundel bevat geen verzend-, antwoord-, doorstuur-, verplaats- of chat-POST-routes.
# Aanroep: ci/bundel_grep.sh <map> (de gecompileerde vanger) [<map met de Python-code>]
set -euo pipefail
fout=0
PATRONEN='sendmail|/send\b|createReply|createReplyAll|createForward|/forward\b|/move\b|childfolders.*POST|\$batch|SendMessage|sendMessage|ChatMessage\.Send|/messages/[^"]*/reply'
if grep -rnEi "$PATRONEN" "$1" ; then echo "::error::verzend- of schrijfroute in de vanger-bundel"; fout=1; else echo "  OK    vanger-bundel zonder verzend-/schrijfroutes"; fi
if grep -rnE "method: *['\"](POST|PATCH|PUT|DELETE)['\"]" "$1"; then echo "::error::schrijvende methode in de vanger-bundel"; fout=1; else echo "  OK    vanger-bundel zonder POST/PATCH/PUT/DELETE"; fi
if [ -n "${2:-}" ]; then
  # In de Python-code mag alleen vangnet.py een verzoek doen; nergens urlopen/requests/http.client buiten vangnet.
  if grep -rnE "urlopen|import requests|http\.client|socket\.create_connection" "$2" | grep -vE "/(vangnet|web)\.py:"; then
    echo "::error::uitgaand verkeer buiten het vangnet"; fout=1; else echo "  OK    Python: uitgaand verkeer alleen via het vangnet"; fi
  # Toegestaan: het vangnet zelf, de servers (api/web: inkomend), de zelftoets (__main__: weigert juist) en precies
  # één vorm van POST: vangnet.verzoek("POST", I.WEBHOOKS[...]) — de vaste n8n-adressen.
  if grep -rnE "\"(POST|PATCH|PUT|DELETE)\"" "$2" | grep -vE "/(vangnet|api|web|__main__)\.py:" \
       | grep -vE 'vangnet\.verzoek\("POST", I\.WEBHOOKS\['; then
    echo "::error::schrijvende methode buiten het vangnet"; fout=1; else echo "  OK    Python: geen schrijvende methode buiten het vangnet"; fi
  if grep -rnE "request\.(post|put|patch|delete|fetch)\(" "$2"; then
    echo "::error::Playwright-verzoek dat geen GET is"; fout=1; else echo "  OK    Python: Playwright alleen request.get"; fi
fi
exit $fout
