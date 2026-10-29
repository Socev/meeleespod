'use strict';
const NAMEN = { outlook: 'Outlook (praktijk)', teams: 'Teams (praktijk)', parro: 'Parro' };
const meldingen = {};
let bezigMet = null;

function tijd(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('nl-NL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
function el(tag, cls, tekst) { const e = document.createElement(tag); if (cls) e.className = cls; if (tekst !== undefined) e.textContent = tekst; return e; }
function zeg(dienst, t) {
  meldingen[dienst] = t;
  const m = document.getElementById('melding-' + dienst);
  if (m) m.textContent = t;
}

async function inloggen(dienst) {
  if (bezigMet) return;
  bezigMet = dienst;
  document.querySelectorAll('button.inlog').forEach((b) => { b.disabled = true; });
  try {
    const r = await fetch('/inloggen?dienst=' + encodeURIComponent(dienst), { method: 'POST', headers: { 'X-Meelees-Knop': '1' } });
    const j = await r.json();
    if (!j.id) { zeg(dienst, j.fout || 'Het inloggen kon niet starten.'); return; }
    zeg(dienst, 'Even controleren of je nog ingelogd bent (tot een paar minuten)…');
    for (let i = 0; i < 400; i++) {
      await new Promise((ok) => setTimeout(ok, 1500));
      const s = await (await fetch('/inloggen/status?id=' + encodeURIComponent(j.id), { headers: { 'X-Meelees-Aanvraag': j.token }, cache: 'no-store' })).json();
      if (s.fase === 'klaar') { zeg(dienst, 'Ingelogd. Er was geen venster nodig, of je bent nu binnen.'); return; }
      if (s.fase === 'mislukt') { zeg(dienst, 'Het inloggen is niet gelukt. Probeer het nog een keer.'); return; }
      if (s.fase === 'venster' && s.code) {
        location.href = '/vnc/?dienst=' + encodeURIComponent(dienst) + '#' + encodeURIComponent(s.code);
        return;
      }
    }
    zeg(dienst, 'Dit duurt te lang. Ververs de pagina.');
  } catch (e) {
    zeg(dienst, 'Het inloggen kon niet starten.');
  } finally {
    bezigMet = null;
    ververs();
  }
}

async function sluitVenster(knop) {
  if (knop) knop.disabled = true;
  try { await fetch('/inloggen/stop', { method: 'POST', headers: { 'X-Meelees-Knop': '1' } }); } catch (e) { /* ververs toont zo de stand */ }
  ververs();
}

function toonOpenVenster(inlogvenster) {
  const bak = document.getElementById('open-venster');
  const tekst = document.getElementById('open-venster-tekst');
  if (!inlogvenster) { bak.hidden = true; return; }
  const naam = NAMEN[inlogvenster.dienst] || inlogvenster.dienst;
  tekst.textContent = 'Er staat een inlogvenster open voor ' + naam + '. Zolang dat openstaat kun je niet opnieuw inloggen. ' +
    'Ben je binnen, of is het venster vastgelopen? Sluit het hier.';
  bak.hidden = false;
  const knop = document.getElementById('open-venster-sluit');
  knop.disabled = false;
  knop.onclick = () => sluitVenster(knop);
}

async function ververs() {
  if (bezigMet) return;
  let s;
  try { s = await (await fetch('/stand.json', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (bezigMet) return;
  toonOpenVenster(s.inlogvenster);
  const bak = document.getElementById('diensten');
  bak.textContent = '';
  for (const d of ['outlook', 'teams', 'parro']) {
    const x = s.diensten[d] || {};
    const blok = el('div', 'dienst');
    blok.appendChild(el('h2', '', NAMEN[d]));
    let toestand, cls;
    if (!x.aan) { toestand = 'staat uit'; cls = 'grijs'; }
    else if (!x.ingelogd_sinds) { toestand = 'nog niet ingelogd'; cls = 'grijs'; }
    else if (x.login_nodig) { toestand = 'uitgelogd — log opnieuw in'; cls = 'slecht'; }
    else { toestand = 'ingelogd' + (x.modus === 'proef' ? ' (proef: er wordt nog niets bewaard)' : ''); cls = 'goed'; }
    blok.appendChild(el('div', 'regel ' + cls, toestand));
    blok.appendChild(el('div', 'regel', 'Ingelogd op: ' + tijd(x.ingelogd_sinds) + ' · geldig tot: ' + tijd(x.geldig_tot)));
    const lr = x.laatste_ronde || {};
    blok.appendChild(el('div', 'regel', 'Laatste ronde: ' + tijd(lr.tijd) + (lr.status ? ' (' + lr.status + ')' : '') +
      ' · laatste met berichten: ' + tijd(x.laatste_met_berichten)));
    if (x.laatste_fout) blok.appendChild(el('div', 'regel slecht', 'Laatste fout: ' + tijd(x.laatste_fout.tijd) + ' ' + x.laatste_fout.code));
    const knop = el('button', 'inlog', 'Inloggen bij ' + NAMEN[d].split(' ')[0]);
    knop.type = 'button';
    knop.disabled = Boolean(s.inlogvenster) || !x.aan;
    knop.addEventListener('click', () => inloggen(d));
    blok.appendChild(knop);
    const m = el('div', 'regel', meldingen[d] || '');
    m.id = 'melding-' + d;
    blok.appendChild(m);
    bak.appendChild(blok);
  }
  toonParroWw(s.parro_wachtwoord);
  document.getElementById('versie').textContent = 'versie ' + s.versie + (s.inlogvenster ? ' · er staat een inlogvenster open (' + s.inlogvenster.dienst + ')' : '');
}
// ---- Parro: zelf opnieuw inloggen (wv1951). Het formulier staat vast in de pagina (ververs() bouwt het niet om),
// de stand toont alleen ja/nee, tijden en uitkomst; gebruikersnaam en wachtwoord komen nooit terug van de pod.
const PROEF = {
  OK: 'gelukt', GEWEIGERD: 'geweigerd door ParnasSys (klopt het wachtwoord?)', NIET_TERUG: 'niet terug in Parro na het inloggen',
  GEEN_API: 'ingelogd, maar Parro haalde niets op', ANDERE_HOST: 'de inlogpagina van ParnasSys verscheen niet',
  GEEN_INLOGPAGINA: 'de inlogpagina van ParnasSys verscheen niet', GEEN_VELD: 'geen invulveld gevonden',
  FORMULIER: 'invullen mislukte', GEEN_WACHTWOORD: 'geen wachtwoord bewaard', SESSIE_NIET_WEG: 'proef zegt niets (sessie bleef staan)',
  BROWSERSTART: 'browser startte niet', BEZET: 'browser bezet', FOUT: 'onbekende fout',
};
function toonParroWw(w) {
  const st = document.getElementById('parro-ww-stand');
  if (!w || !w.bewaard) { st.className = 'regel grijs'; st.textContent = 'Geen wachtwoord bewaard.' + (w && w.gewist ? ' Gewist op ' + tijd(w.gewist) + '.' : ''); return; }
  let t = 'Bewaard op ' + tijd(w.gezet) + '.';
  let cls = 'regel goed';
  if (w.geweigerd) { t += ' ParnasSys weigerde het op ' + tijd(w.geweigerd) + ': vul het opnieuw in.'; cls = 'regel slecht'; }
  else if (w.gepauzeerd) { t += ' Zelf inloggen mislukte ' + w.fouten_op_rij + ' keer op rij en staat uit: vul het opnieuw in.'; cls = 'regel slecht'; }
  else if (w.budget_op) { t += ' Zelf inloggen mislukte ' + w.mislukt_24u + ' keer in 24 uur en wacht nu (bescherming tegen blokkeren van je account).'; cls = 'regel slecht'; }
  if (w.proef) {
    if (!w.proef.code) t += ' Proefinlog loopt…';
    else t += ' Proefinlog ' + tijd(w.proef.tijd) + ': ' + (PROEF[w.proef.code] || w.proef.code) + '.';
    if (w.proef.code && w.proef.code !== 'OK' && !w.geweigerd) cls = 'regel slecht';
  } else if (!w.geweigerd && !w.gepauzeerd) t += ' Proefinlog loopt (± 1 minuut)…';
  if (w.laatst_gelukt) t += ' Laatst zelf ingelogd: ' + tijd(w.laatst_gelukt) + '.';
  st.className = cls;
  st.textContent = t;
}
async function bewaarParroWw(ev) {
  ev.preventDefault();
  const g = document.getElementById('parro-ww-gebruiker');
  const w = document.getElementById('parro-ww-wachtwoord');
  const m = document.getElementById('parro-ww-melding');
  const knop = document.getElementById('parro-ww-bewaar');
  knop.disabled = true;
  try {
    const r = await fetch('/parro/wachtwoord', { method: 'POST', headers: { 'X-Meelees-Knop': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ gebruiker: g.value, wachtwoord: w.value }) });
    const j = await r.json();
    if (r.ok && j.ok) {
      g.value = ''; w.value = '';
      m.textContent = 'Bewaard. De pod probeert nu één keer zelf in te loggen in een lege browser (± 1 minuut); de uitkomst verschijnt hierboven.';
    } else m.textContent = j.fout || 'Bewaren lukte niet.';
  } catch (e) { m.textContent = 'Bewaren lukte niet.'; } finally { knop.disabled = false; ververs(); }
}
async function wisParroWw() {
  const m = document.getElementById('parro-ww-melding');
  try {
    const r = await fetch('/parro/wachtwoord/wis', { method: 'POST', headers: { 'X-Meelees-Knop': '1' } });
    m.textContent = r.ok ? 'Gewist.' : 'Wissen lukte niet.';
  } catch (e) { m.textContent = 'Wissen lukte niet.'; } finally { ververs(); }
}
document.getElementById('parro-ww-form').addEventListener('submit', bewaarParroWw);
document.getElementById('parro-ww-bewaar').disabled = false;   // pas aan als het formulier via JS gaat (review ZOU 5)
document.getElementById('parro-ww-wis').addEventListener('click', wisParroWw);

ververs();
setInterval(ververs, 5000);
