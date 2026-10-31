// meeleespod — vanger: ALLEEN de inlogvangst van outlook-access (cb4d299) en teams-access (1aa084d).
//
// Aanroep (door de Python-kant, nooit door een mens):
//   node dist/vanger/src/vanger.js <outlook|teams> <zichtbaar|stil> <uitvoermap>
// Omgeving: HOME = de geheime map van die dienst (teams-access schrijft daar .teams-cli/), MEELEES_PROFIEL = het
// browserprofiel, DISPLAY = het virtuele scherm.
//
// Wat eruit komt:
//   - <uitvoermap>/lezer.json (0600): alleen wat de eigen leesclient nodig heeft (token(s), vervaltijd, en voor Outlook de
//     cookies van outlook.office.com). Geen inlogcookies van login.microsoftonline.com.
//   - één regel JSON op stdout: {ok, dienst, code, geldig_tot, ...} — NOOIT een token.
//   - exitcode 0 ok, 4 inloggen nodig, 5 tijdelijk (netwerk, browserstart), 1 anders.
// Alles wat de pakketten op stderr schrijven, gooit de Python-kant weg (logfilter, witte lijst).
//
// Browserbeleid (ontwerp § 6): de pakketten starten Chromium zelf. Daarom wordt playwright.chromium.launchPersistentContext
// hier omwikkeld vóór de pakketcode laadt: geen downloads, kiosk (geen adresbalk, geen tabbladen), eigen Chromium (geen
// Google Chrome-kanaal), en een popup naar een host buiten de inloglijst gaat meteen dicht. Het beleidsbestand in het
// image (ontwikkelaarsgereedschap uit, downloads uit, chrome://- en file://-adressen geblokkeerd) doet de rest.

import * as fs from 'node:fs';
import * as path from 'node:path';

type Dienst = 'outlook' | 'teams';
type Modus = 'zichtbaar' | 'stil';

const UIT_OK = 0;
const UIT_FOUT = 1;
const UIT_LOGIN = 4;
const UIT_TIJDELIJK = 5;

// Hosts waar een popup heen mag tijdens het inloggen (Microsoft-inlog en de Microsoft-apps zelf).
const POPUP_HOSTS = [
  /(^|\.)microsoftonline\.com$/i,
  /(^|\.)microsoft\.com$/i,
  /(^|\.)live\.com$/i,
  /(^|\.)office\.com$/i,
  /(^|\.)office365\.com$/i,
  /(^|\.)office\.net$/i,
  /(^|\.)cloud\.microsoft$/i,
  /(^|\.)mcas\.ms$/i,
  /(^|\.)msauth\.net$/i,
  /(^|\.)msftauth\.net$/i,
  // wv1958: Teams-web haalt zijn schil van *.static.microsoft (teams.public.onecdn.static.microsoft); een popup daarheen
  // ging dicht. Alle andere Microsoft-domeinen van de opdracht stonden er al.
  /(^|\.)static\.microsoft$/i,
];

// ---- Teams-meting (wv1958) -------------------------------------------------------------------------------------------
// David 29-10-2026: Teams-web toonde "tijdelijk geen chats", herlaadde vanzelf, kwam op een inlogscherm dat bleef
// verversen, en daarna ONVOLLEDIG (geen chatsvcagg/ic3). Om dat van buiten te kunnen onderscheiden meet de vanger voor
// Teams alleen soorten en aantallen: nooit een url, tekst of token. Lijsten staan dubbel (meeleespod/vangst.py);
// tests/test_teams_meting.py houdt ze gelijk.
const HOST_KLASSEN = ['TEAMS_MCAS', 'TEAMS', 'MCAS_MELDING', 'MS_INLOG', 'OUTLOOK', 'M365', 'STATIC', 'ANDERS'] as const;
type HostKlasse = (typeof HOST_KLASSEN)[number];
// Audiences van een MSAL-toegangstoken in localStorage, op naam (alleen of er een geldig token is).
const MSAL_NAMEN: Array<[string, string]> = [
  ['chatsvcagg', 'chatsvcagg\\.teams\\.microsoft\\.com'],
  ['ic3', 'ic3\\.teams\\.office\\.com'],
  ['graph', 'graph\\.microsoft\\.com'],
  ['presence', 'presence\\.teams\\.microsoft\\.com'],
  ['spaces', 'api\\.spaces\\.skype\\.com'],
  ['outlook', 'outlook\\.office(365)?\\.com'],
  ['substrate', 'substrate\\.office\\.com'],
];
// De foutmelding "chats tijdelijk niet beschikbaar" (en + nl: de taal van Teams volgt het account, niet de browser).
// Alleen het ja/nee verlaat de pagina. Treffer in een meldingselement (role=alert/status/dialog, aria-live) = sterk;
// elders in de pagina = zwak (gewone chattekst kan zo'n zin bevatten; telt apart en zet het scherm niet).
const CHATS_FOUT_RE = [
  "(temporar(y|ily)|right now|at the moment)[^.\\n]{0,40}\\b(chats|messages|conversations)\\b",
  "\\b(chats|messages|conversations)\\b[^.\\n]{0,40}(temporarily|unavailable|can['’]?t be (loaded|shown|displayed)|couldn['’]?t (be )?load)",
  "(couldn['’]?t|can['’]?t|cannot|unable to|trouble|problem) (load|loading|show|showing|display|get|getting|retriev\\w*)[^.\\n]{0,30}\\b(chats|messages|conversations)\\b",
  "tijdelijk[^.\\n]{0,40}(chats|berichten|gesprekken)",
  "(chats|berichten|gesprekken)[^.\\n]{0,40}(tijdelijk niet|niet beschikbaar|niet (worden )?(geladen|gelezen|weergegeven))",
  "(kunnen|kan) [^.\\n]{0,40}(chats|berichten|gesprekken)[^.\\n]{0,30}niet (laden|lezen|ophalen|weergeven)",
];

function hostKlasse(url: string): HostKlasse {
  let h: string;
  try {
    h = new URL(url).hostname.toLowerCase();
  } catch {
    return 'ANDERS';
  }
  if (h === 'mcas-proxyweb.mcas.ms') return 'MCAS_MELDING';
  if (/^teams\.(cloud\.microsoft|microsoft\.com)\.mcas\.ms$/.test(h)) return 'TEAMS_MCAS';
  if (/(^|\.)teams\.(cloud\.microsoft|microsoft\.com)$/.test(h)) return 'TEAMS';
  if (/(^|\.)(login\.microsoftonline\.com|login\.live\.com|login\.microsoft\.com)$/.test(h)) return 'MS_INLOG';
  if (/(^|\.)outlook\.office(365)?\.com(\.mcas\.ms)?$/.test(h)) return 'OUTLOOK';
  if (/(^|\.)(m365\.cloud\.microsoft|office\.com)(\.mcas\.ms)?$/.test(h)) return 'M365';
  if (/(^|\.)static\.microsoft$/.test(h)) return 'STATIC';
  return 'ANDERS';
}

const diag = {
  metingen: 0,
  nav: {} as Record<string, number>, // navigaties van het hoofdframe per hostklasse (ook de eigen rondgang van het pakket)
  zelfde_adres: 0, // hoofdframe twee keer op rij naar hetzelfde adres (zonder #)
  herlaad: 0, // documenten die door een herlading ontstonden (performance navigation type "reload")
  popup_dicht: {} as Record<string, number>, // door de popup-sluiter gesloten pagina's per hostklasse
  chats_fout: { melding: 0, tekst: 0 }, // metingen met de chatfoutmelding: in een meldingselement / elders in de pagina
  msal: {} as Record<string, string[]>, // per hostklasse: audiences met een geldig toegangstoken in localStorage
  rt: [] as string[], // hostklassen waar een MSAL-vernieuwtoken in localStorage stond
};
const herlaadDocs = new Set<number>();
let teamsMeten = false;

function tel(r: Record<string, number>, k: string): void {
  r[k] = (r[k] ?? 0) + 1;
}

// Draait IN de pagina (page.evaluate): zelfstandig, geeft alleen getallen, namen uit de lijst en ja/nee terug.
function paginaMeting(arg: { fout: string[]; auds: Array<[string, string]> }): { fout: number; aud: string[]; rt: boolean; herlaad: number } {
  const g = globalThis as unknown as {
    document?: { body?: { innerText?: string }; querySelectorAll: (s: string) => ArrayLike<{ innerText?: string }> };
    localStorage?: { length: number; key: (i: number) => string | null; getItem: (k: string) => string | null };
    performance?: { timeOrigin: number; getEntriesByType: (t: string) => Array<{ type?: string }> };
  };
  let fout = 0;
  try {
    const res = arg.fout.map((s) => new RegExp(s, 'i'));
    const d = g.document;
    if (d) {
      const els = Array.from(d.querySelectorAll('[role=alert],[role=alertdialog],[role=status],[role=dialog],[aria-live]'));
      const kort = els.map((e) => e.innerText || '').join('\n');
      if (res.some((r) => r.test(kort))) fout = 2;
      else if (res.some((r) => r.test((d.body && d.body.innerText) || ''))) fout = 1;
    }
  } catch {
    /* geen meting */
  }
  const aud = new Set<string>();
  let rt = false;
  try {
    const ls = g.localStorage;
    const nu = Date.now() / 1000;
    if (ls) {
      for (let i = 0; i < ls.length; i++) {
        const k = ls.key(i) || '';
        if (/refreshtoken/i.test(k)) rt = true;
        if (!/accesstoken/i.test(k)) continue;
        let v: { target?: unknown; expiresOn?: unknown; expires_on?: unknown } = {};
        try {
          v = JSON.parse(ls.getItem(k) || '{}');
        } catch {
          continue;
        }
        const exp = Number(v.expiresOn ?? v.expires_on ?? 0);
        if (exp && exp < nu) continue;
        const t = `${String(v.target ?? '')} ${k}`;
        for (const [naam, re] of arg.auds) if (new RegExp(re, 'i').test(t)) aud.add(naam);
      }
    }
  } catch {
    /* geen meting */
  }
  let herlaad = 0;
  try {
    const p = g.performance;
    if (p && p.getEntriesByType('navigation').some((e) => e.type === 'reload')) herlaad = Math.floor(p.timeOrigin);
  } catch {
    /* geen meting */
  }
  return { fout, aud: Array.from(aud), rt, herlaad };
}

type MeetBlad = { url: () => string; isClosed: () => boolean; evaluate: <R, A>(f: (a: A) => R, a: A) => Promise<R> };

async function meetBlad(p: MeetBlad): Promise<void> {
  if (p.isClosed()) return;
  const k = hostKlasse(p.url());
  if (k === 'MS_INLOG' || k === 'MCAS_MELDING' || k === 'STATIC') return; // geen app-opslag; inlogvelden laten we met rust
  let m;
  try {
    m = await p.evaluate(paginaMeting, { fout: CHATS_FOUT_RE, auds: MSAL_NAMEN });
  } catch {
    return; // pagina navigeert of is dicht; de volgende meting probeert opnieuw
  }
  diag.metingen++;
  if (k === 'TEAMS' || k === 'TEAMS_MCAS') {
    if (m.fout === 2) diag.chats_fout.melding++;
    else if (m.fout === 1) diag.chats_fout.tekst++;
  }
  if (m.herlaad && !herlaadDocs.has(m.herlaad)) {
    herlaadDocs.add(m.herlaad);
    diag.herlaad++;
  }
  const namen = MSAL_NAMEN.map(([n]) => n);
  const al = new Set(diag.msal[k] ?? []);
  for (const n of m.aud) if (namen.includes(n)) al.add(n);
  if (al.size) diag.msal[k] = namen.filter((n) => al.has(n));
  if (m.rt && !diag.rt.includes(k)) diag.rt.push(k);
}

async function meetAlles(): Promise<void> {
  if (!teamsMeten || !laatsteCtx) return;
  try {
    await Promise.all((laatsteCtx.pages() as unknown as MeetBlad[]).map((p) => meetBlad(p)));
  } catch {
    /* context dicht */
  }
}

function volgNavigatie(p: { on: (e: string, f: (fr: unknown) => void) => void; mainFrame: () => unknown; url: () => string }): void {
  let vorige = '';
  p.on('framenavigated', (fr: unknown) => {
    if (fr !== p.mainFrame()) return;
    const u = p.url();
    tel(diag.nav, hostKlasse(u));
    const a = u.split('#')[0];
    if (a && a === vorige) diag.zelfde_adres++;
    vorige = a;
  });
}

// ---- Microsoft-inlogtelling (wv2066) ---------------------------------------------------------------------------------
// David 31-10-2026: Outlook viel ± 36 u na de inlog uit; vaker stil vangen moet laten zien of dat een vaste termijn van
// de tenant is of inactiviteit. Daarvoor telt de vanger per vangst de verzoeken van de browser aan de Microsoft-inlog
// (hostklasse MS_INLOG): authorize (sessiecookie), token (vernieuwtoken), overig, en hoeveel daarvan faalden. Van een
// foutantwoord alleen de foutnaam uit MS_FOUTNAMEN en de AADSTS-nummers (bv. 70044 = aanmeldfrequentie). Nooit een url,
// header, code of token: van een geslaagd antwoord wordt niets gelezen, van een redirect alleen de Location op die twee
// patronen, en een antwoordtekst alleen bij status >= 400 van het token-eindpunt (een foutantwoord draagt geen token).
// Lijst gelijk aan meeleespod/vangst.py (MS_FOUTNAMEN); tests/test_warm.py houdt ze gelijk.
const MS_FOUTNAMEN = ['login_required', 'interaction_required', 'consent_required', 'invalid_grant', 'invalid_request',
  'temporarily_unavailable', 'server_error'] as const;
const msTel = { authorize: 0, token: 0, overig: 0, fout: 0, fouten: {} as Record<string, number>, aadsts: [] as number[] };

function msEindpunt(url: string): 'authorize' | 'token' | 'overig' | null {
  if (hostKlasse(url) !== 'MS_INLOG') return null;
  let p: string;
  try {
    p = new URL(url).pathname.toLowerCase();
  } catch {
    return null;
  }
  if (/\/oauth2\/(v2\.0\/)?authorize$/.test(p)) return 'authorize';
  if (/\/oauth2\/(v2\.0\/)?token$/.test(p)) return 'token';
  return 'overig';
}

// Alleen de foutnaam (lijst) en AADSTS-nummers uit een stuk tekst; de tekst zelf verlaat deze functie niet.
function msFoutUit(tekst: string): { naam: string | null; nummers: number[] } {
  let naam: string | null = null;
  const m = /(?:^|[?#&"\s,{])error(?:"\s*:\s*"|=)([a-z_]{1,40})/.exec(tekst);
  if (m) naam = (MS_FOUTNAMEN as readonly string[]).includes(m[1]) ? m[1] : 'ANDERS';
  const nummers: number[] = [];
  for (const x of tekst.matchAll(/AADSTS(\d{5,7})/g)) {
    const n = Number(x[1]);
    if (!nummers.includes(n)) nummers.push(n);
  }
  return { naam, nummers };
}

function msNoteer(f: { naam: string | null; nummers: number[] }): void {
  if (!f.naam && !f.nummers.length) return;
  msTel.fout++;
  if (f.naam) tel(msTel.fouten, f.naam);
  for (const n of f.nummers) if (!msTel.aadsts.includes(n) && msTel.aadsts.length < 8) msTel.aadsts.push(n);
}

type MsVerzoek = { url: () => string };
type MsAntwoord = { url: () => string; status: () => number; headers: () => Record<string, string>; text: () => Promise<string> };

function volgMsInlog(ctx: { on: (e: string, f: (x: never) => void) => void }): void {
  ctx.on('request', (r: MsVerzoek) => {
    try {
      const soort = msEindpunt(r.url());
      if (soort) msTel[soort]++;
    } catch {
      /* best effort */
    }
  });
  ctx.on('response', (a: MsAntwoord) => {
    void (async () => {
      try {
        const soort = msEindpunt(a.url());
        if (!soort) return;
        const st = a.status();
        if (st >= 300 && st < 400) {
          // prompt=none mislukt: redirect naar de app met error=… in query of fragment
          msNoteer(msFoutUit(a.headers()['location'] || ''));
        } else if (st >= 400) {
          if (soort === 'token') {
            const f = msFoutUit((await a.text()).slice(0, 20000));
            msNoteer(f.naam || f.nummers.length ? f : { naam: 'ANDERS', nummers: [] });
          } else {
            msNoteer({ naam: 'ANDERS', nummers: [] });
          }
        }
      } catch {
        /* antwoord al weg of geen tekst: niet tellen */
      }
    })();
  });
}

// Teams: alleen deze audiences gaan naar de lezer (ontwerp § 4: "alleen de audiences die nodig zijn").
const TEAMS_AUD: Record<string, string> = {
  chatsvcagg: 'https://chatsvcagg.teams.microsoft.com',
  ic3: 'https://ic3.teams.office.com',
};
// Graph wordt NIET bewaard; alleen gemeten of het token Mail.Read draagt (ontwerp § 4: Graph als tweede leesroute).
const GRAPH_AUD = 'https://graph.microsoft.com';

function klaar(uitslag: Record<string, unknown>, code: number): never {
  if (browserGestart) uitslag.ms = msTel; // wv2066: Microsoft-inlogtelling bij elke uitslag
  process.stdout.write(JSON.stringify(uitslag) + '\n');
  process.exit(code);
}

function schrijfGeheim(map: string, naam: string, inhoud: unknown): void {
  fs.mkdirSync(map, { recursive: true, mode: 0o700 });
  const doel = path.join(map, naam);
  const tmp = `${doel}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(inhoud), { mode: 0o600 });
  fs.chmodSync(tmp, 0o600);
  fs.renameSync(tmp, doel);
}

function hostToegestaan(url: string): boolean {
  if (url === 'about:blank' || url === '') return true;
  try {
    const h = new URL(url).hostname;
    return POPUP_HOSTS.some((re) => re.test(h));
  } catch {
    return false;
  }
}

// Wist de vanger of Chromium al draaide toen het misging? (wv1950: BROWSERSTART zei niet óf de start faalde.)
let browserGestart = false;
// De context (voor de schermmeting bij een fout) en hoe vaak de vanger zelf een MCAS-melding wegklikte.
type Blad = { url: () => string };
let laatsteCtx: { pages: () => Blad[] } | null = null;
let mcasGeklikt = 0;
// Teams: het scherm vlak vóór het pakket de browser sloot (daarna zijn er geen pagina's meer om te meten).
let eindScherm: string | null = null;

// Op welk soort scherm stond de browser toen het misging? Vaste witte lijst, nooit de url zelf (wv1950: Outlook
// stil vernieuwen bleef sinds 28-10 hangen; inlogscherm en MCAS-melding zijn van buiten niet te onderscheiden).
function schermSoort(): string {
  // Teams (wv1958): de chatfoutmelding telt als ze in de loop van de vangst ergens zichtbaar was; aan het eind staat het
  // pakket na zijn rondgang meestal op office.com, dus het eindscherm zegt dan niets meer over Teams.
  if (teamsMeten && diag.chats_fout.melding > 0) return 'TEAMS_CHATS_FOUT';
  try {
    const urls = laatsteCtx ? laatsteCtx.pages().map((p) => p.url()) : [];
    if (urls.some((u) => /mcas-proxyweb\.mcas\.ms|certificate-checker/i.test(u))) return 'MCAS_MELDING';
    if (urls.some((u) => /login\.microsoftonline\.com|login\.live\.com|\/signin/i.test(u))) return 'MS_INLOG';
    if (urls.some((u) => /^https:\/\/[^/]*(outlook|teams)\.[^/]*\//i.test(u))) return 'APP';
    return urls.length ? 'ANDERS' : 'GEEN';
  } catch {
    return 'ANDERS';
  }
}

// Outlook: de MCAS-proxy van de tenant toont soms een "omleidingsmelding" (mcas-proxyweb.mcas.ms/certificate-checker,
// zonder inlogveld). teams-access klikt die weg ("Proceed to"), outlook-access niet: dat wacht er net als op een
// inlogscherm tot de time-out (browser-capture.ts onSignIn). Daarom klikt de vanger voor Outlook die ene link zelf, op nl
// ("Doorgaan naar") en en ("Proceed to"). Alleen op die melding, alleen die link; nooit een inlogveld.
function klikMcasMelding(p: { on: (e: string, f: (fr: unknown) => void) => void; mainFrame: () => unknown; url: () => string; locator: (s: string) => { first: () => { isVisible: (o?: object) => Promise<boolean>; click: (o?: object) => Promise<void> } } }): void {
  let bezig = false;
  p.on('framenavigated', (fr: unknown) => {
    if (fr !== p.mainFrame() || bezig) return;
    if (!/mcas-proxyweb\.mcas\.ms|certificate-checker/i.test(p.url())) return;
    bezig = true;
    void (async () => {
      try {
        const link = p.locator('a:has-text("Doorgaan naar"), a:has-text("Proceed to")').first();
        for (let i = 0; i < 10 && !(await link.isVisible()); i++) await new Promise((r) => setTimeout(r, 500));
        if (await link.isVisible()) {
          await link.click({ timeout: 8000 });
          mcasGeklikt++;
        }
      } catch {
        /* best effort: lukt het niet, dan meldt de schermmeting MCAS_MELDING */
      } finally {
        bezig = false;
      }
    })();
  });
}

// Subcode bij BROWSERSTART: vaste witte lijst, nooit fouttekst of paden (zelfde lijst en volgorde als
// meeleespod/vangst.py browser_subcode). De slotmelding staat in de browserlogs ván een "Target ... closed"-fout, dus eerst.
function browserSubcode(bericht: string): string {
  if (/ProcessSingleton|profile appears to be in use|SingletonLock|existing browser session/i.test(bericht)) return 'SLOT_IN_GEBRUIK';
  if (/Executable doesn't exist|error while loading shared libraries|ENOENT|Cannot find module/i.test(bericht)) return 'GEEN_BINARY';
  if (/Missing X server|cannot open display|\$DISPLAY/i.test(bericht)) return 'GEEN_SCHERM';
  if (/ENOMEM|out of memory|Cannot allocate memory/i.test(bericht)) return 'GEHEUGEN';
  if (/ENOSPC|No space left/i.test(bericht)) return 'SCHIJF_VOL';
  if (/Timeout \d+ms exceeded/i.test(bericht)) return 'STARTTIJD';
  if (/Target page, context or browser has been closed|has been closed/i.test(bericht)) return 'DOEL_GESLOTEN';
  return 'ANDERS';
}

function zetBeleid(modus: Modus, dienst: Dienst): void {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const pw = require('playwright');
  const origineel = pw.chromium.launchPersistentContext.bind(pw.chromium);
  pw.chromium.launchPersistentContext = async (map: string, opties: Record<string, unknown> = {}) => {
    const o: Record<string, unknown> = { ...opties };
    delete o.channel; // altijd de Chromium uit het image, nooit een Chrome-kanaal
    o.headless = false; // altijd op het virtuele scherm (Teams vernieuwt headless niet op een tenant met voorwaardelijke toegang)
    // Teams: forceer Engels (Accept-Language). Op een Defender-for-Cloud-Apps-tenant leidt de eerste Teams-navigatie
    // via de MCAS-proxy om naar een "omleidingsmelding" (mcas-proxyweb.mcas.ms/certificate-checker). Het pakket klikt die
    // melding weg met de tekst "Proceed to ..." (teams-access browser-capture.ts clearMcasInterstitial). De proxy rendert
    // die pagina in de taal uit Accept-Language: met nl-NL staat er "Doorgaan naar ..." en matcht de selector NIET, dus
    // bleef het inlogvenster op de melding hangen tot de time-out (gemeten 27-10-2026, B2 wv1716). Met en-US staat er
    // "Proceed to ..." en ruimt het pakket de melding zelf op. Gemeten 27-10 vanuit de claudebot-pod: zelfde IP, nl→NL,
    // en→EN, dus de taal volgt Accept-Language. Outlook logde in nl-NL al goed in; die laten we ongemoeid.
    if (dienst === 'teams') o.locale = 'en-US';
    o.acceptDownloads = false;
    o.viewport = null;
    const args = Array.isArray(o.args) ? [...(o.args as string[])] : [];
    args.push('--kiosk', '--window-position=0,0', '--window-size=1280,900', '--no-first-run', '--disable-sync');
    o.args = args;
    const ctx = await origineel(map, o);
    browserGestart = true;
    laatsteCtx = ctx;
    volgMsInlog(ctx);
    if (dienst === 'outlook') {
      for (const p of ctx.pages()) klikMcasMelding(p);
      ctx.on('page', klikMcasMelding);
    }
    if (dienst === 'teams') {
      // wv1958: meten (alleen soorten en aantallen). Elke 4 s de open pagina's, en één keer vlak vóór het sluiten.
      teamsMeten = true;
      for (const p of ctx.pages()) volgNavigatie(p);
      ctx.on('page', volgNavigatie);
      const tik = setInterval(() => void meetAlles(), 4000);
      if (typeof tik.unref === 'function') tik.unref();
      const sluit = ctx.close.bind(ctx);
      ctx.close = async (...a: unknown[]) => {
        clearInterval(tik);
        await meetAlles();
        eindScherm = schermSoort();
        return sluit(...(a as []));
      };
    }
    ctx.on('page', async (p: { opener: () => Promise<unknown>; url: () => string; close: () => Promise<void>; waitForLoadState: (s: string, o: object) => Promise<void> }) => {
      try {
        const opener = await p.opener();
        if (!opener) return; // pagina's die de pakketcode zelf opent
        try {
          await p.waitForLoadState('domcontentloaded', { timeout: 5000 });
        } catch {
          /* geen probleem: dan oordelen we op de huidige url */
        }
        if (!hostToegestaan(p.url())) {
          tel(diag.popup_dicht, hostKlasse(p.url()));
          await p.close();
        }
      } catch {
        /* best effort */
      }
    });
    void modus;
    return ctx;
  };
}

function soortFout(e: unknown): { code: string; exit: number; sub?: string } {
  const bericht = e instanceof Error ? e.message : String(e);
  const c = (e as { code?: unknown; exitCode?: unknown }) || {};
  if (/ERR_INTERNET|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|ERR_CERT|ERR_NETWORK|ERR_TIMED_OUT|ERR_PROXY/i.test(bericht)) {
    return { code: 'TIJDELIJK', exit: UIT_TIJDELIJK };
  }
  if (/launchPersistentContext|Executable doesn't exist|Target page, context or browser has been closed|browserType\.launch/i.test(bericht)) {
    return { code: 'BROWSERSTART', exit: UIT_TIJDELIJK, sub: browserSubcode(bericht) };
  }
  if (c.code === 'LOGIN_TIMEOUT' || c.code === 'NO_TOKEN' || c.code === 'AUTH_CANCELLED' || c.exitCode === 4) {
    return { code: 'LOGIN_NODIG', exit: UIT_LOGIN };
  }
  // Teams meldt "geen bearer binnen de tijd" als tekst; een kale 'timeout' (traag netwerk) is GEEN uitgelogd (review).
  if (/no bearer|not authenticated|auth(entication)? required|login timeout/i.test(bericht)) {
    return { code: 'LOGIN_NODIG', exit: UIT_LOGIN };
  }
  return { code: 'FOUT', exit: UIT_FOUT };
}

async function vangOutlook(modus: Modus, uitMap: string, profiel: string): Promise<never> {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { captureOutlookSession } = require('../../vendor/outlook-access/src/auth/browser-capture');
  const r = await captureOutlookSession({
    profileDir: profiel,
    chromeChannel: 'chromium',
    loginTimeoutMs: modus === 'zichtbaar' ? 600_000 : 60_000,
    headless: false,
  });
  const cookies = (r.cookies as Array<{ name: string; value: string; domain: string }>)
    .filter((c) => {
      const d = (c.domain || '').toLowerCase().replace(/^\./, '');
      return d === 'outlook.office.com' || d.endsWith('.outlook.office.com');
    })
    .map((c) => ({ name: c.name, value: c.value, domain: c.domain }));
  schrijfGeheim(uitMap, 'lezer.json', {
    dienst: 'outlook',
    token: r.bearer.token,
    geldig_tot: r.bearer.expiresAt,
    audience: r.bearer.audience,
    anchorMailbox: r.anchorMailbox,
    cookies,
    gevangen_op: new Date().toISOString(),
  });
  klaar({ ok: true, dienst: 'outlook', code: 'OK', geldig_tot: r.bearer.expiresAt, mcas: mcasGeklikt }, UIT_OK);
}

async function vangTeams(modus: Modus, uitMap: string, profiel: string): Promise<never> {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { captureSession } = require('../../vendor/teams-access/src/auth/browser-capture');
  const opties: Record<string, unknown> = {
    loginTimeoutMs: modus === 'zichtbaar' ? 600_000 : 90_000,
    chromeChannel: 'chromium',
    profileDir: profiel,
    headless: false,
    // na de vangst nog even open om alle audiences te zien; zichtbaar korter (venster als David = zo kort mogelijk)
    diagnosticExtraMs: modus === 'zichtbaar' ? 25_000 : 40_000,
  };
  if (modus === 'stil') {
    // zelfde drempels als teams-cli auth-renew (auth-renew.ts RENEW_EVICT_BELOW_TTL_S / RENEW_FLOOR_TTL_S)
    opties.evictBelowTtlS = 1500;
    opties.captureFloorTtlS = 900;
  }
  const home = process.env.HOME || '';
  const ruimOp = (): void => {
    // De pakketcode schreef alles (alle audiences + alle cookies) in $HOME/.teams-cli: altijd weg, ook na een fout.
    for (const f of ['session.json', 'multi-tokens.json', 'login-trace.jsonl', 'multi-tokens.json.tmp']) {
      try {
        fs.rmSync(path.join(home, '.teams-cli', f), { force: true });
      } catch {
        /* best effort */
      }
    }
  };
  let s;
  try {
    s = await captureSession(opties);
  } finally {
    ruimOp();
  }
  const tokens: Record<string, { token: string; exp?: number }> = {};
  const aanwezig: Record<string, boolean> = {};
  for (const [naam, aud] of Object.entries(TEAMS_AUD)) {
    const t = s.tokens?.[aud];
    aanwezig[naam] = Boolean(t && t.bearerToken);
    if (t && t.bearerToken) tokens[naam] = { token: t.bearerToken, exp: t.exp };
  }
  const g = s.tokens?.[GRAPH_AUD];
  aanwezig.graph = Boolean(g && g.bearerToken);
  const mailRead = Boolean(g && typeof g.scp === 'string' && /(^|\s)Mail\.Read(\s|$)/.test(g.scp));
  if (!aanwezig.chatsvcagg || !aanwezig.ic3) {
    klaar({ ok: false, dienst: 'teams', code: 'ONVOLLEDIG', audiences: aanwezig, scherm: eindScherm ?? schermSoort(), diag }, UIT_LOGIN);
  }
  const exps = Object.values(tokens).map((t) => t.exp).filter((x): x is number => typeof x === 'number');
  const geldigTot = exps.length ? new Date(Math.min(...exps) * 1000).toISOString() : null;
  schrijfGeheim(uitMap, 'lezer.json', {
    dienst: 'teams',
    tokens,
    region: { csa: s.region?.csa ?? null, chatsvc: s.region?.chatsvc ?? null },
    geldig_tot: geldigTot,
    gevangen_op: new Date().toISOString(),
  });
  klaar({ ok: true, dienst: 'teams', code: 'OK', geldig_tot: geldigTot, audiences: aanwezig, graph_mail_read: mailRead, diag }, UIT_OK);
}

async function hoofd(): Promise<void> {
  const [dienst, modus, uitMap] = process.argv.slice(2) as [Dienst, Modus, string];
  if (!['outlook', 'teams'].includes(dienst) || !['zichtbaar', 'stil'].includes(modus) || !uitMap) {
    klaar({ ok: false, code: 'GEBRUIK' }, UIT_FOUT);
  }
  const profiel = process.env.MEELEES_PROFIEL || '';
  if (!profiel) klaar({ ok: false, dienst, code: 'GEEN_PROFIEL' }, UIT_FOUT);
  zetBeleid(modus, dienst);
  // outlook-access maakt de vangbelofte vóór de navigatielus en wacht er pas ná op; vuurt zijn time-out tijdens die lus
  // (inlogscherm of MCAS-melding), dan is die afwijzing onafgevangen en stopte Node met exit 1 zonder uitslag (gemeten
  // 29-10-2026 10:01Z, EXIT_1). Hier wordt zo'n afwijzing gewoon een uitslag (LOGIN_TIMEOUT → LOGIN_NODIG).
  const fout = (e: unknown): void => {
    const s = soortFout(e);
    const uit: Record<string, unknown> = { ok: false, dienst, code: s.code, gestart: browserGestart, scherm: eindScherm ?? schermSoort(), mcas: mcasGeklikt };
    if (s.sub) uit.sub = s.sub;
    if (teamsMeten) uit.diag = diag;
    klaar(uit, s.exit);
  };
  process.on('unhandledRejection', fout);
  process.on('uncaughtException', fout);
  try {
    if (dienst === 'outlook') await vangOutlook(modus, uitMap, profiel);
    else await vangTeams(modus, uitMap, profiel);
  } catch (e) {
    fout(e);
  }
}

// Alleen als programma starten; de toets (ci/teams_meting_toets.cjs) laadt de meetfuncties los.
if (require.main === module) void hoofd();
const eindSchermVoorToets = (): string | null => eindScherm;
export { hostKlasse, paginaMeting, CHATS_FOUT_RE, MSAL_NAMEN, HOST_KLASSEN, zetBeleid, diag, eindSchermVoorToets, msEindpunt, msFoutUit, msNoteer, msTel, MS_FOUTNAMEN };
