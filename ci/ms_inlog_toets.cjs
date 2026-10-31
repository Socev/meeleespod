// wv2066: toets van de Microsoft-inlogtelling in vanger.ts (msTel) tegen een echte Chromium, met nagemaakte antwoorden
// (alles onderschept, verzonnen codes en tokens; nooit een echte Microsoft-host). Aanroep:
//   node ci/ms_inlog_toets.cjs <pad naar vanger.js>
// Omgeving: MEELEES_TOETS_CHROMIUM_VOL = pad naar een volledige Chromium (optioneel; anders die van Playwright).
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const assert = require('node:assert/strict');
const vanger = require(path.resolve(process.argv[2]));
const { chromium } = require(require.resolve('playwright', { paths: [path.dirname(path.resolve(process.argv[2])), process.cwd()] }));

(async () => {
  // eindpunten
  const e = vanger.msEindpunt;
  assert.equal(e('https://login.microsoftonline.com/tid/oauth2/v2.0/authorize?client_id=x'), 'authorize');
  assert.equal(e('https://login.microsoftonline.com/common/oauth2/authorize'), 'authorize');
  assert.equal(e('https://login.microsoftonline.com/tid/oauth2/v2.0/token'), 'token');
  assert.equal(e('https://login.microsoftonline.com/common/reprocess'), 'overig');
  assert.equal(e('https://outlook.office.com/owa/'), null);
  assert.equal(e('geen url'), null);
  // foutnaam en AADSTS-nummers; de tekst zelf komt nooit terug
  const f = vanger.msFoutUit;
  assert.deepEqual(f('https://outlook.office.com/mail/#error=login_required&error_description=AADSTS50058%3a+verzonnen'), { naam: 'login_required', nummers: [50058] });
  assert.deepEqual(f('{"error":"invalid_grant","error_description":"AADSTS70043: verzonnen","error_codes":[70043]}'), { naam: 'invalid_grant', nummers: [70043] });
  assert.deepEqual(f('https://x/#error=iets_nieuws&error_description=AADSTS700084+en+AADSTS700084'), { naam: 'ANDERS', nummers: [700084] });
  assert.deepEqual(f('https://x/#code=verzonnen&state=y'), { naam: null, nummers: [] });
  assert.deepEqual(f('https://x/?foo_error=login_required'), { naam: null, nummers: [] });

  // browser: het beleid van de vanger (zetBeleid) hangt de telling aan de context
  vanger.zetBeleid('stil', 'outlook');
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), 'ms-inlog-'));
  const ctx = await chromium.launchPersistentContext(prof, {
    executablePath: process.env.MEELEES_TOETS_CHROMIUM_VOL || undefined,
    args: ['--headless=new'],
  });
  const app = 'https://outlook.office.com/mail/';
  const cors = { 'access-control-allow-origin': 'https://outlook.office.com' };
  await ctx.route('**/*', (r) => {
    const u = r.request().url();
    if (u.startsWith(app)) {
      return r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: `<!doctype html><body>app
<iframe src="https://login.microsoftonline.com/tid/oauth2/v2.0/authorize?prompt=none&n=1"></iframe>
<iframe src="https://login.microsoftonline.com/tid/oauth2/v2.0/authorize?prompt=none&n=2"></iframe>
<script>fetch('https://login.microsoftonline.com/tid/oauth2/v2.0/token',{method:'POST',body:'refresh_token=verzonnen-rt'}).catch(()=>{});
fetch('https://login.microsoftonline.com/tid/oauth2/v2.0/token?n=ok',{method:'POST',body:'refresh_token=verzonnen-rt2'}).catch(()=>{});</script></body>` });
    }
    if (/authorize\?prompt=none&n=1/.test(u)) {
      return r.fulfill({ status: 302, headers: { location: `${app}blank#error=interaction_required&error_description=AADSTS70044%3a+verzonnen+sessie` } });
    }
    if (/authorize\?prompt=none&n=2/.test(u)) {
      return r.fulfill({ status: 302, headers: { location: `${app}blank#code=verzonnen-code&state=s` } });
    }
    if (/oauth2\/v2\.0\/token\?n=ok/.test(u)) {
      return r.fulfill({ status: 200, headers: cors, contentType: 'application/json', body: '{"access_token":"eyJ.verzonnen.x","refresh_token":"verzonnen-nieuw"}' });
    }
    if (/oauth2\/v2\.0\/token/.test(u)) {
      return r.fulfill({ status: 400, headers: cors, contentType: 'application/json', body: '{"error":"invalid_grant","error_description":"AADSTS700084: verzonnen","error_codes":[700084]}' });
    }
    return r.fulfill({ status: 200, contentType: 'text/html', body: '<p>x</p>' });
  });
  const p = ctx.pages()[0] || (await ctx.newPage());
  await p.goto(app);
  for (let i = 0; i < 40 && !(vanger.msTel.token >= 2 && vanger.msTel.authorize >= 2 && vanger.msTel.fout >= 2); i++) await p.waitForTimeout(250);
  await p.waitForTimeout(500);
  await ctx.close();
  const t = vanger.msTel;
  assert.equal(t.authorize, 2, JSON.stringify(t));
  assert.equal(t.token, 2, JSON.stringify(t));
  assert.equal(t.fout, 2, JSON.stringify(t));
  assert.deepEqual(t.fouten, { interaction_required: 1, invalid_grant: 1 });
  assert.deepEqual([...t.aadsts].sort((a, b) => a - b), [70044, 700084]);
  assert.ok(!/verzonnen|eyJ|http|code|state/i.test(JSON.stringify(t)), JSON.stringify(t));
  console.log('ms_inlog_toets: ok');
})().catch((e) => {
  console.error('ms_inlog_toets: MISLUKT', e && e.message);
  process.exit(1);
});
