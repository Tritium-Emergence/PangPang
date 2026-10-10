// Durable send idempotency — pure, testable. The SAME unconfirmed send must reuse ONE
// Idempotency-Key across retries (so a network-ambiguous retry dedupes server-side), not
// mint a fresh UUID. The fingerprint is the STABLE user intent (bot + text + refs +
// explicit continue target) — NOT volatile routing hints. The pending slot is PER-BOT so
// two bots' in-flight sends never overwrite each other's key.
//
// Storage and the UUID source are injected so this is unit-testable without a browser.

export interface KV {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
  removeItem(k: string): void;
}

export const pendingKey = (botId: string) => 'pending-send:' + botId;

export interface SendIntent {text: string; refs: string[]; continueWorkId: string | null}

async function fingerprint(botId: string, intent: SendIntent, subtle: SubtleCrypto): Promise<string> {
  const payload = JSON.stringify({text: intent.text, refs: intent.refs, continue_work_id: intent.continueWorkId});
  const bytes = await subtle.digest('SHA-256', new TextEncoder().encode(botId + payload));
  return Array.from(new Uint8Array(bytes), (x) => x.toString(16).padStart(2, '0')).join('');
}

// Return the Idempotency-Key for this send, reusing the per-bot persisted key iff the
// stable-intent fingerprint matches the pending slot; otherwise mint and persist a new one.
export async function sendKeyFor(
  botId: string, intent: SendIntent,
  store: KV, subtle: SubtleCrypto, uuid: () => string,
): Promise<string> {
  const fp = await fingerprint(botId, intent, subtle);
  const stored = JSON.parse(store.getItem(pendingKey(botId)) || 'null');
  const key = stored && stored.fingerprint === fp ? stored.key: uuid();
  store.setItem(pendingKey(botId), JSON.stringify({fingerprint: fp, key}));
  return key;
}

export function clearPending(botId: string, store: KV): void {
  store.removeItem(pendingKey(botId));
}
