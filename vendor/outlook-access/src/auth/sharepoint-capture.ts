// EIGEN VERVANGING (meeleespod), NIET van upstream. Het origineel (sharepoint-capture.ts op cb4d299) vangt een
// SharePoint-sessie; de pod gebruikt SharePoint niet en de bijbehorende download-route stuurde de sleutel naar elke
// URL (nalezing wv1435). browser-capture.ts importeert deze functie wel; zonder sharepointHost wordt hij nooit
// aangeroepen. Deze stub weigert altijd, zodat er nooit een SharePoint-sessie in de pod ontstaat.
import type { BrowserContext } from 'playwright';
import type { SharepointSession } from '../session/sharepoint-schema';

export async function captureSharepointFromContext(
  _context: BrowserContext,
  _host: string,
  _timeoutMs: number,
): Promise<SharepointSession> {
  throw new Error('sharepoint-vangst staat uit in de meeleespod');
}
