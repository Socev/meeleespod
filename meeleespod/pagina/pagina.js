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
  document.getElementById('versie').textContent = 'versie ' + s.versie + (s.inlogvenster ? ' · er staat een inlogvenster open (' + s.inlogvenster.dienst + ')' : '');
}
ververs();
setInterval(ververs, 5000);
