// wv1958: toets van de Teams-meting in vanger.ts tegen een echte Chromium, met nagemaakte pagina's (alles onderschept,
// verzonnen tokens; nooit een echte Microsoft-host). Aanroep: node ci/teams_meting_toets.cjs <pad naar vanger.js>
// Omgeving: MEELEES_TOETS_CHROMIUM = pad naar een Chromium (optioneel; anders die van Playwright).
const path = require('node:path');
const assert = require('node:assert/strict');
const vanger = require(path.resolve(process.argv[2]));
const { chromium } = require(require.resolve('playwright', { paths: [path.dirname(path.resolve(process.argv[2])), process.cwd()] }));

const nu = Math.floor(Date.now() / 1000);
const jwtLoos = 'x.eyJhdWQiOiJ2ZXJ6b25uZW4ifQ.x';
function pagina(banner, extra) {
  const ls = {
    'msal.2|acc|login.windows.net|accesstoken|cid|tid|https://chatsvcagg.teams.microsoft.com/.default': { secret: jwtLoos, target: 'https://chatsvcagg.teams.microsoft.com/.default', expiresOn: String(nu + 3600) },
    'msal.2|acc|login.windows.net|accesstoken|cid|tid|https://ic3.teams.office.com/.default': { secret: jwtLoos, target: 'https://ic3.teams.office.com/.default', expiresOn: String(nu - 60) },
    'msal.2|acc|login.windows.net|refreshtoken|cid||': { secret: 'verzonnen' },
    'iets-anders': 'x',
  };
  return `<!doctype html><html><body><div>Chat met verzonnen collega: ik ben tijdelijk afwezig, stuur een bericht</div>
${banner ? `<div role="alert">${banner}</div>` : ''}${extra || ''}
<script>const d=${JSON.stringify(ls)};for(const k in d)localStorage.setItem(k,typeof d[k]==='string'?d[k]:JSON.stringify(d[k]));</script></body></html>`;
}

(async () => {
  // hostklassen
  const hk = vanger.hostKlasse;
  assert.equal(hk('https://teams.cloud.microsoft.mcas.ms/v2/?view=Chat'), 'TEAMS_MCAS');
  assert.equal(hk('https://teams.microsoft.com/v2/'), 'TEAMS');
  assert.equal(hk('https://mcas-proxyweb.mcas.ms/certificate-checker?x'), 'MCAS_MELDING');
  assert.equal(hk('https://login.microsoftonline.com/common/oauth2'), 'MS_INLOG');
  assert.equal(hk('https://outlook.office365.com.mcas.ms/mail/'), 'OUTLOOK');
  assert.equal(hk('https://m365.cloud.microsoft/'), 'M365');
  assert.equal(hk('https://www.office.com/'), 'M365');
  assert.equal(hk('https://teams.public.onecdn.static.microsoft/x'), 'STATIC');
  assert.equal(hk('https://voorbeeld.invalid/'), 'ANDERS');
  assert.equal(hk('geen url'), 'ANDERS');

  const browser = await chromium.launch({ executablePath: process.env.MEELEES_TOETS_CHROMIUM || undefined });
  try {
    const ctx = await browser.newContext({ locale: 'en-US' });
    const inhoud = {
      'https://teams.cloud.microsoft.mcas.ms/v2/a': pagina("We're temporarily unable to load your chats. Try again later."),
      'https://teams.cloud.microsoft.mcas.ms/v2/b': pagina('Er kunnen tijdelijk geen chats worden geladen.'),
      'https://teams.cloud.microsoft.mcas.ms/v2/c': pagina('', '<div>Your chats are temporarily unavailable</div>'),
      'https://teams.cloud.microsoft.mcas.ms/v2/d': pagina('Welcome to Teams'),
      'https://teams.cloud.microsoft.mcas.ms/v2/e': pagina('', '<div>Unable to load messages from the server</div>'),
    };
    await ctx.route('**/*', (r) => {
      const u = r.request().url();
      if (inhoud[u]) return r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: inhoud[u] });
      return r.fulfill({ status: 404, body: '' });
    });
    const p = await ctx.newPage();
    const arg = { fout: vanger.CHATS_FOUT_RE, auds: vanger.MSAL_NAMEN };
    const verwacht = { a: 2, b: 2, c: 1, d: 0, e: 1 };
    for (const [k, v] of Object.entries(verwacht)) {
      await p.goto(`https://teams.cloud.microsoft.mcas.ms/v2/${k}`);
      const m = await p.evaluate(vanger.paginaMeting, arg);
      assert.equal(m.fout, v, `pagina ${k}`);
      assert.deepEqual(m.aud, ['chatsvcagg'], `pagina ${k}: alleen het geldige token telt`);
      assert.equal(m.rt, true);
      assert.equal(m.herlaad, 0);
      // de meting geeft nooit tekst, url of token terug
      assert.ok(!/tijdelijk|temporar|verzonnen|eyJ|http/i.test(JSON.stringify(m)), JSON.stringify(m));
    }
    await p.reload();
    const m = await p.evaluate(vanger.paginaMeting, arg);
    assert.ok(m.herlaad > 0, 'herlading gezien');
  } finally {
    await browser.close();
  }

  // Deel 2: het browserbeleid van de vanger zelf (zetBeleid) met de Teams-meting: navigaties, popup-sluiter, meting vlak
  // vóór het sluiten. zetBeleid zet headless uit; --headless=new in args houdt de toets zonder scherm.
  vanger.zetBeleid('stil', 'teams');
  const prof = require('node:fs').mkdtempSync(require('node:path').join(require('node:os').tmpdir(), 'teams-meting-'));
  const ctx2 = await chromium.launchPersistentContext(prof, {
    executablePath: process.env.MEELEES_TOETS_CHROMIUM_VOL || undefined,
    args: ['--headless=new'],
  });
  const banner = pagina("We're temporarily unable to load your chats.");
  await ctx2.route('**/*', (r) => r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: /teams\.cloud/.test(r.request().url()) ? banner : '<p>x</p>' }));
  const q = ctx2.pages()[0] || (await ctx2.newPage());
  await q.goto('https://teams.cloud.microsoft.mcas.ms/v2/?view=Chat');
  await q.waitForTimeout(4600);
  await q.goto('https://teams.cloud.microsoft.mcas.ms/v2/?view=Chat');
  const dicht = ctx2.waitForEvent('page');
  await q.evaluate(() => { window.open('https://voorbeeld.invalid/popup'); });
  const pop = await dicht;
  const open = ctx2.waitForEvent('page');
  await q.evaluate(() => { window.open('https://teams.public.onecdn.static.microsoft/schil'); });
  const popStatic = await open;
  await q.waitForTimeout(1500);
  assert.equal(pop.isClosed(), true, 'popup buiten de lijst dicht');
  assert.equal(popStatic.isClosed(), false, 'popup naar static.microsoft blijft open (wv1958)');
  await q.goto('https://login.microsoftonline.com/common/oauth2/authorize');
  await ctx2.close();
  const d = vanger.diag;
  assert.ok(d.nav.TEAMS_MCAS >= 2 && d.nav.MS_INLOG === 1, JSON.stringify(d.nav));
  assert.ok(d.zelfde_adres >= 1, 'zelfde adres');
  assert.deepEqual(d.popup_dicht, { ANDERS: 1 });
  assert.ok(d.chats_fout.melding >= 1, 'chatfoutmelding gezien');
  assert.deepEqual(d.msal.TEAMS_MCAS, ['chatsvcagg']);
  assert.ok(d.rt.includes('TEAMS_MCAS'));
  assert.ok(d.metingen >= 1);
  assert.equal(vanger.eindSchermVoorToets(), 'TEAMS_CHATS_FOUT');
  assert.ok(!/temporar|verzonnen|eyJ|http|invalid/i.test(JSON.stringify(d)), JSON.stringify(d));
  console.log('teams_meting_toets: ok');
})().catch((e) => {
  console.error('teams_meting_toets: MISLUKT', e && e.message);
  process.exit(1);
});
