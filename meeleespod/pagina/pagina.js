'use strict';
const NAMEN = { outlook: 'Outlook (praktijk)', teams: 'Teams (praktijk)', parro: 'Parro' };
function tijd(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('nl-NL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
function el(tag, cls, tekst) { const e = document.createElement(tag); if (cls) e.className = cls; if (tekst !== undefined) e.textContent = tekst; return e; }
async function inloggen(dienst, knop) {
  knop.disabled = true;
  try {
    const r = await fetch('/inloggen?dienst=' + encodeURIComponent(dienst), { method: 'POST', headers: { 'X-Meelees-Knop': '1' } });
    const j = await r.json();
    if (j.code) { location.href = '/vnc/?dienst=' + encodeURIComponent(dienst) + '#' + encodeURIComponent(j.code); return; }
    alert(j.fout || 'Het inlogvenster kon niet starten.');
  } catch (e) { alert('Het inlogvenster kon niet starten.'); }
  knop.disabled = false;
}
async function ververs() {
  let s;
  try { s = await (await fetch('/stand.json', { cache: 'no-store' })).json(); } catch (e) { return; }
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
    const knop = el('button', '', 'Inloggen bij ' + NAMEN[d].split(' ')[0]);
    knop.type = 'button';
    if (s.inlogvenster) knop.disabled = true;
    knop.addEventListener('click', () => inloggen(d, knop));
    blok.appendChild(knop);
    bak.appendChild(blok);
  }
  document.getElementById('versie').textContent = 'versie ' + s.versie + (s.inlogvenster ? ' · er staat een inlogvenster open (' + s.inlogvenster.dienst + ')' : '');
}
ververs();
setInterval(ververs, 5000);
