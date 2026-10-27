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
];

// Teams: alleen deze audiences gaan naar de lezer (ontwerp § 4: "alleen de audiences die nodig zijn").
const TEAMS_AUD: Record<string, string> = {
  chatsvcagg: 'https://chatsvcagg.teams.microsoft.com',
  ic3: 'https://ic3.teams.office.com',
};
// Graph wordt NIET bewaard; alleen gemeten of het token Mail.Read draagt (ontwerp § 4: Graph als tweede leesroute).
const GRAPH_AUD = 'https://graph.microsoft.com';

function klaar(uitslag: Record<string, unknown>, code: number): never {
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

function zetBeleid(modus: Modus): void {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const pw = require('playwright');
  const origineel = pw.chromium.launchPersistentContext.bind(pw.chromium);
  pw.chromium.launchPersistentContext = async (map: string, opties: Record<string, unknown> = {}) => {
    const o: Record<string, unknown> = { ...opties };
    delete o.channel; // altijd de Chromium uit het image, nooit een Chrome-kanaal
    o.headless = false; // altijd op het virtuele scherm (Teams vernieuwt headless niet op een tenant met voorwaardelijke toegang)
    o.acceptDownloads = false;
    o.viewport = null;
    const args = Array.isArray(o.args) ? [...(o.args as string[])] : [];
    args.push('--kiosk', '--window-position=0,0', '--window-size=1280,900', '--no-first-run', '--disable-sync');
    o.args = args;
    const ctx = await origineel(map, o);
    ctx.on('page', async (p: { opener: () => Promise<unknown>; url: () => string; close: () => Promise<void>; waitForLoadState: (s: string, o: object) => Promise<void> }) => {
      try {
        const opener = await p.opener();
        if (!opener) return; // pagina's die de pakketcode zelf opent
        try {
          await p.waitForLoadState('domcontentloaded', { timeout: 5000 });
        } catch {
          /* geen probleem: dan oordelen we op de huidige url */
        }
        if (!hostToegestaan(p.url())) await p.close();
      } catch {
        /* best effort */
      }
    });
    void modus;
    return ctx;
  };
}

function soortFout(e: unknown): { code: string; exit: number } {
  const bericht = e instanceof Error ? e.message : String(e);
  const c = (e as { code?: unknown; exitCode?: unknown }) || {};
  if (/ERR_INTERNET|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|ERR_CERT|ERR_NETWORK|ERR_TIMED_OUT|ERR_PROXY/i.test(bericht)) {
    return { code: 'TIJDELIJK', exit: UIT_TIJDELIJK };
  }
  if (/launchPersistentContext|Executable doesn't exist|Target page, context or browser has been closed|browserType\.launch/i.test(bericht)) {
    return { code: 'BROWSERSTART', exit: UIT_TIJDELIJK };
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
  klaar({ ok: true, dienst: 'outlook', code: 'OK', geldig_tot: r.bearer.expiresAt }, UIT_OK);
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
    klaar({ ok: false, dienst: 'teams', code: 'ONVOLLEDIG', audiences: aanwezig }, UIT_LOGIN);
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
  klaar({ ok: true, dienst: 'teams', code: 'OK', geldig_tot: geldigTot, audiences: aanwezig, graph_mail_read: mailRead }, UIT_OK);
}

async function hoofd(): Promise<void> {
  const [dienst, modus, uitMap] = process.argv.slice(2) as [Dienst, Modus, string];
  if (!['outlook', 'teams'].includes(dienst) || !['zichtbaar', 'stil'].includes(modus) || !uitMap) {
    klaar({ ok: false, code: 'GEBRUIK' }, UIT_FOUT);
  }
  const profiel = process.env.MEELEES_PROFIEL || '';
  if (!profiel) klaar({ ok: false, dienst, code: 'GEEN_PROFIEL' }, UIT_FOUT);
  zetBeleid(modus);
  try {
    if (dienst === 'outlook') await vangOutlook(modus, uitMap, profiel);
    else await vangTeams(modus, uitMap, profiel);
  } catch (e) {
    const s = soortFout(e);
    klaar({ ok: false, dienst, code: s.code }, s.exit);
  }
}

void hoofd();
