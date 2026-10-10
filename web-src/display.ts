import type {TFunction} from 'i18next';
import i18n from './i18n';
import type {Routine} from './types';

// Presentation only: map server codes to i18n keys; never adjudicate Work reality.
// Values are STATIC i18n keys (never call t() at module load — that would freeze the
// language). Consumers hold a live t from useTranslation() and translate at render.
export const attentionLabel: Record<string, string> = {needs_attention: 'attention.needs_attention', blocked: 'attention.blocked', working: 'attention.working', done_unread: 'attention.done_unread', settled: 'attention.settled', unknown: 'attention.unknown'};
export const taskLabel: Record<string, string> = {queued: 'task.queued', pending: 'task.pending', running: 'task.running', starting: 'task.starting', awaiting_approval: 'task.awaiting_approval', unknown: 'task.unknown', cancel_requested: 'task.cancel_requested', succeeded: 'task.succeeded', failed: 'task.failed', cancelled: 'task.cancelled', blocked: 'task.blocked'};
export const exchangeLabel: Record<string, string> = {pending: 'exchange.pending', responded: 'exchange.responded', failed: 'exchange.failed', cancelled: 'exchange.cancelled'};
export const DEFAULT_OUTPUT_TOKENS = 32768; // pangpang/model_config.py: default and minimum output cap
export const capabilityLabel: Record<string, string> = {model: 'capability.model', executor: 'capability.executor', device: 'capability.device', web_search: 'capability.web_search', semantic_gate: 'capability.semantic_gate'};
export const capabilityStateLabel: Record<string, string> = {ready: 'capabilityState.ready', unconfigured: 'capabilityState.unconfigured', blocked: 'capabilityState.blocked', unknown: 'capabilityState.unknown'};
// The server seeds a new Work's checkpoint with phase "new"; later phases are
// model-written free text (service.py:1953, 200-char, no enum). Only known phases
// are localized; an unknown model-written phase passes through verbatim (RCB).
const PHASE_KEYS: Record<string, string> = {new: 'work.phase.new', delivered: 'work.phase.delivered'};
export const checkpointView = (cp: Record<string, unknown> | undefined, t: TFunction = i18n.t) =>
  cp && typeof cp.phase === 'string' && PHASE_KEYS[cp.phase] ? {...cp, phase: t(PHASE_KEYS[cp.phase])}: cp;
export function clock(n?: number) {
  if (!n) return i18n.t('format.empty');
  const d = new Date(n * 1000), hm = d.toLocaleTimeString(i18n.language, {hour: '2-digit', minute: '2-digit', hour12: false});
  return d.toDateString() === new Date().toDateString() ? hm: `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}
export const dateTime = (n?: number) => n ? new Date(n * 1000).toLocaleString(i18n.language, {hour12: false}): i18n.t('format.empty');
export const fileSize = (n?: number | null) => n == null ? i18n.t('format.sizeUnknown'): n < 1024 ? `${n} B`: n < 1048576 ? `${(n / 1024).toFixed(1)} KB`: `${(n / 1048576).toFixed(1)} MB`;
export function scheduleText(r: Routine, t: TFunction = i18n.t) {
  const s = r.schedule || {};
  const text = s.type === 'daily' ? t('format.daily', {time: s.time}): s.type === 'weekly' ? t('format.weekly', {weekdays: (s.weekdays || []).join(','), time: s.time}): s.type === 'interval' ? t('format.interval', {minutes: s.minutes}): s.type === 'one-shot' ? t('format.oneShot', {at: s.at}): JSON.stringify(s);
  return `${text} · ${r.timezone || t('format.noTimezone')}`;
}
// friendlyError: known codes get a localized sentence with the raw code appended for
// inspectability; unknown codes return the raw code verbatim (RCB — never crash or
// show an internal exception string). t defaults to i18n.t so event-callback callers
// (store.setNotice etc.) work without threading a hook-bound t.
export function friendlyError(e: unknown, t: TFunction = i18n.t) {
  const code = e instanceof Error ? e.message: String(e || 'operation_failed');
  const label = t(`errors.${code}`, {defaultValue: ''});
  return label ? `${label}（${code}）`: code;
}
