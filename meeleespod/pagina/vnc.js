import RFB from '/novnc/core/rfb.js';
const code = decodeURIComponent((location.hash || '').slice(1));
history.replaceState(null, '', location.pathname + location.search);   // code niet in de geschiedenis laten staan
const dienst = new URLSearchParams(location.search).get('dienst') || '';
const melding = document.getElementById('melding');
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
let rfb;
function zeg(t) { melding.textContent = t; }
if (!code) { zeg('Geen code: start het venster opnieuw vanaf de pagina.'); }
else {
  rfb = new RFB(document.getElementById('scherm'), proto + '//' + location.host + '/vnc/ws?code=' + encodeURIComponent(code));
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.addEventListener('connect', () => zeg('Log in. Het venster sluit vanzelf zodra je binnen bent.'));
  rfb.addEventListener('disconnect', () => { zeg('Venster dicht.'); setTimeout(() => { location.href = '/'; }, 2500); });
}
document.getElementById('stop').addEventListener('click', async () => {
  await fetch('/inloggen/stop', { method: 'POST', headers: { 'X-Meelees-Knop': '1' } });
  location.href = '/';
});
setInterval(async () => {
  try {
    const s = await (await fetch('/stand.json', { cache: 'no-store' })).json();
    if (!s.inlogvenster) { zeg('Klaar. Terug naar de pagina…'); setTimeout(() => { location.href = '/'; }, 1500); }
  } catch (e) { /* volgende keer */ }
}, 4000);
void dienst;
