"""Vaste instellingen van de meeleespod.

De webhook-adressen staan HIER, vast in het image (ontwerp § 6): wie de Socev-sleutel heeft, kan de pod alleen sleutels
geven (/api/koppel), nooit een ander adres. Een ander adres = nieuwe commit, nieuw image, nieuwe chart.
"""
import os
from pathlib import Path

# ---- uitgaand: precies deze adressen voor POST (n8n-webhooks; dezelfde als de praktijk-pc, repo socev-pc) ----------
WEBHOOKS = {
    "outlook": "https://n8n.huisdokter.dev/webhook/outlook-classificatie",
    "teams": "https://n8n.huisdokter.dev/webhook/teams-classificatie",
    "parro": "https://n8n.huisdokter.dev/webhook/parro-meelezen-q8t3n",
    # bijlagenpad (ontwerp § 5.3); de n8n-kant komt in B3. Staat uit tot MEELEES_BIJLAGEN=aan.
    "outlook-bijlage": "https://n8n.huisdokter.dev/webhook/outlook-bijlage",
}

# ---- uitgaand: GET alleen naar deze hosts (lezers). Chromium zelf valt hier buiten (zie LEESMIJ: egress). ----------
GET_HOSTS = frozenset({
    "outlook.office.com",       # Outlook REST v2.0 (eigen dunne leesclient)
    "teams.microsoft.com",      # Teams: chatsvc en csa (chats, berichten, kanaalposts)
    "rest-v2.parro.com",        # Parro
})

# Welke sleutel bij welke webhook hoort (de pc-namen: meelees = Outlook + Teams, parro = Parro)
SLEUTEL_VOOR = {"outlook": "meelees", "teams": "meelees", "outlook-bijlage": "meelees", "parro": "parro"}
SLEUTELNAMEN = ("meelees", "parro")

USER_AGENT = "Socev-meelezen/2 (meeleespod)"   # Cloudflare voor huisdokter.dev weigert de standaard-UA van urllib

DIENSTEN = ("outlook", "teams", "parro")

# ---- paden -------------------------------------------------------------------------------------------------------
STAAT = Path(os.environ.get("MEELEES_STAAT", "/staat"))     # app-data: waterlijnen, gezien, oordelen, sleutels
GEHEIM = Path(os.environ.get("MEELEES_GEHEIM", "/geheim"))  # app-cache: sessies en browserprofielen
WERK = Path(os.environ.get("MEELEES_WERK", "/werk"))        # tmpfs: bijlagen heel even (pdftotext wil een bestand)
VANGER = os.environ.get("MEELEES_VANGER", "/app/vanger/dist/vanger/src/vanger.js")
NOVNC = Path(os.environ.get("MEELEES_NOVNC", "/app/novnc"))

VERSIE = os.environ.get("MEELEES_VERSIE") or (Path("/app/GIT_SHA").read_text().strip()[:12] if Path("/app/GIT_SHA").exists() else "dev")
SCRIPT_VERSIE = f"pod-{VERSIE}"


def aan(naam, standaard="aan"):
    return os.environ.get(naam, standaard).strip().lower() in ("aan", "1", "ja", "true")


# Per dienst aan/uit (chart). Een dienst die nog nooit is ingelogd draait sowieso niet en alarmeert niet.
DIENST_AAN = {d: aan(f"MEELEES_{d.upper()}", "aan") for d in DIENSTEN}
BIJLAGEN_AAN = aan("MEELEES_BIJLAGEN", "uit")   # pas aan na B3 (n8n-tak /outlook-bijlage)

# Ritme (ontwerp § 4): Outlook en Teams elk uur 07-23 en om 03:00, Parro elke 2 uur 07-21.
UREN = {
    "outlook": tuple(range(7, 24)) + (3,),
    "teams": tuple(range(7, 24)) + (3,),
    "parro": tuple(range(7, 22, 2)),
}
MINUUT = {"outlook": 5, "teams": 15, "parro": 25}

EERSTE_VENSTER_DAGEN = int(os.environ.get("MEELEES_EERSTE_VENSTER_DAGEN", "7"))
# Parro zaaien (ontwerp § 8.2): alles van vóór de laatste pc-ronde geldt als al verstuurd.
PARRO_ZAAI_TOT = os.environ.get("MEELEES_PARRO_ZAAI_TOT", "2026-10-25T15:17:00+01:00")

VERNIEUW_ONDER_S = 20 * 60          # token korter dan 20 min geldig: stil vernieuwen
INLOGVENSTER_MAX_S = 10 * 60        # zichtbaar venster sluit uiterlijk na 10 min
VNC_CODE_GELDIG_S = 120             # eenmalige code: binnen 2 min gebruiken, één verbinding
RONDE_REM_S = 10 * 60               # /api/ronde: 1x per 10 min per dienst
LEVENSTEKEN_UUR = 6
ALARM_HERHAAL_UUR = {"login": 24, "inlogvenster": 0, "vooraankondiging": 24 * 365}
# login: bij de overgang en daarna hooguit 1x per 24 u; inlogvenster: elke start; vooraankondiging: één keer
ALARM_INTERVAL_UUR = 6              # overige alarmen: 1x per 6 u per soort (zoals de pc)
VOORAANKONDIGING_DAGEN = 80         # Microsoft-inlogcookie leeft ± 90 dagen

DISPLAY = os.environ.get("MEELEES_DISPLAY", ":99")
SCHERM = "1280x900x24"
