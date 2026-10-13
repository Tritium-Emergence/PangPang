// DOM-agnostic, React-free pure helpers. The single source of truth for the
// security- and RCB-sensitive logic the Layer-B node tests lock: production code
// imports these directly, and the tests import the SAME module (no bundle slicing,
// no esbuild/vm, no second implementation).

// XSS gate for rendered links/images: allow http(s), site-relative,./ relative and
// data:image only; everything else (javascript:/vbscript:/file:/data:text/html) -> ''.
export function safeUrl(u: string): string {
  u = String(u || '').trim();
  return /^(https?:\/\/|\/|\.\/|data:image\/)/i.test(u) ? u: '';
}

// Defensive unwrap: a model sometimes echoes the whole context frame back as its reply
// text, burying the real answer in.content. If a string parses to such a frame, show
// only.content.
export function unwrapFrame(content: unknown): unknown {
  if (typeof content !== 'string') return content;
  const t = content.trim();
  if (t[0] !== '{' || !t.includes('"content"')) return content;
  try {
    const f = JSON.parse(t);
    if (f && typeof f === 'object' && typeof f.content === 'string' && 'kind' in f && ('evidence_ref' in f || 'source' in f)) {
      return f.content;
    }
  } catch { /* not a frame */ }
  return content;
}

const MEDIA_EXT: Record<string, string[]> = {
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'],
  video: ['mp4', 'webm', 'mov'],
  audio: ['mp3', 'wav', 'ogg', 'm4a'],
};

// Classify an artifact by file extension (case-insensitive); unknown -> 'other'.
export function mediaKind(name: string): string {
  const e = String(name).split('.').pop()!.toLowerCase();
  for (const k in MEDIA_EXT) if (MEDIA_EXT[k].includes(e)) return k;
  return 'other';
}

export interface PreviewState {text: string; rev: number; done: boolean}
export interface PreviewFrame {text?: string; delta?: string; rev?: number; base_rev?: number; done?: boolean}

// Ephemeral stream reducer. A `delta` extends the text this client already
// holds at base_rev. A full `text` replaces it (reconnect, or a new assistant
// message after a tool round). A gap drops the suffix instead of inventing
// text; the durable snapshot is what remains after `done`.
export function applyPreviewFrame(cur: PreviewState | undefined, frame: PreviewFrame): PreviewState | undefined {
  if (frame.done) {
    if (!cur) return undefined;
    return cur.done ? cur : {...cur, done: true};
  }
  if (typeof frame.rev !== 'number') return cur;
  if (cur && frame.rev <= cur.rev) return cur;
  if (typeof frame.delta === 'string') {
    if (!cur || frame.base_rev !== cur.rev) return cur;
    return {text: cur.text + frame.delta, rev: frame.rev, done: false};
  }
  if (typeof frame.text === 'string') return {text: frame.text, rev: frame.rev, done: false};
  return cur;
}

// message.changed does not alter the dashboard snapshot. Any other event,
// including a missing type or a reset, still requires the full snapshot.
export function mergeRefreshScope(pending: 'all' | 'messages', eventType: string | undefined): 'all' | 'messages' {
  return eventType === 'message.changed' ? pending : 'all';
}

export const PER_PAGE = 10;

export interface ActivitySummaryTask {
  status?: string;
  created?: number;
  started?: number;
  required_outputs?: unknown;
  activity?: {
    status?: string;
    last_log_at?: number | null;
    observation_capped?: string;
    recent?: {tool?: string; path?: string}[];
    present_outputs?: {path?: string; present?: boolean}[];
  } | null;
}

type Tr = (key: string, vars?: Record<string, unknown>) => string;

function clockHMS(unix: number): string {
  const d = new Date(unix * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// Absolute queue or start clock, last log time, latest step, and required files
// already present. The snapshot activity has no age_s; that field stays on the planner packet.
// starting uses its own clock — started is recorded before the process is running.
export function activitySummary(task: ActivitySummaryTask | null | undefined, tr: Tr): string | null {
  const activity = task?.activity;
  if (!activity) return null;
  const status = String(activity.status || task?.status || '');
  const anchor = status === 'queued' ? task?.created
    : (status === 'starting' || status === 'running') ? task?.started
    : undefined;
  const clock = typeof anchor === 'number' && anchor > 0 ? clockHMS(anchor) : '';
  const parts: string[] = [];
  if (clock && status === 'queued') parts.push(tr('workpane.activityQueued', {time: clock}));
  else if (clock && status === 'starting') parts.push(tr('workpane.activityStarting', {time: clock}));
  else if (clock && status === 'running') parts.push(tr('workpane.activityRunning', {time: clock}));
  if (typeof activity.last_log_at === 'number' && activity.last_log_at > 0) {
    parts.push(tr('workpane.activityLastLog', {time: clockHMS(activity.last_log_at)}));
  }
  const recent = Array.isArray(activity.recent) ? activity.recent : [];
  const step = recent.length ? recent[recent.length - 1] : null;
  if (step && typeof step.tool === 'string' && step.tool) {
    parts.push(tr('workpane.activityRecent', {step: step.path ? `${step.tool} ${step.path}` : step.tool}));
  }
  const declared = task?.required_outputs;
  const undeclared = !Array.isArray(declared) || declared.length === 0;
  const outputs = Array.isArray(activity.present_outputs) ? activity.present_outputs : [];
  if (undeclared) parts.push(tr('workpane.activityUndeclared'));
  else {
    const files = outputs.filter((item) => item && item.present && item.path).map((item) => item.path);
    if (files.length) parts.push(tr('workpane.activityPresent', {files: files.join(', ')}));
  }
  if (activity.observation_capped === 'maybe') parts.push(tr('workpane.activityCapped'));
  const observed = (typeof activity.last_log_at === 'number' && activity.last_log_at > 0)
    || recent.some((item) => item && item.tool)
    || outputs.some((item) => item && item.present);
  if (!observed) parts.push(tr('workpane.activityNone'));
  return parts.join(' · ');
}

// Text the operator typed when manually resolving a task. A system termination
// proof is a different field and is not this statement.
export function operatorStatement(task: {operator_evidence?: unknown; receipt?: unknown} | null | undefined): string | null {
  if (!task || typeof task !== 'object') return null;
  const receipt = task.receipt;
  const fromReceipt = receipt && typeof receipt === 'object' && !Array.isArray(receipt)
    ? (receipt as {operator_evidence?: unknown}).operator_evidence
    : undefined;
  const nested = typeof fromReceipt === 'string' ? fromReceipt.trim() : '';
  const direct = typeof task.operator_evidence === 'string' ? task.operator_evidence.trim() : '';
  const text = nested || direct;
  return text || null;
}

// Pure pagination math: clamp the requested page into range and compute the
// slice bounds. usePager() consumes this so the grid window + clamp are tested once,
// independent of the React scroll-anchor effect.
export function pageWindow(total: number, requestedPage: number, perPage: number = PER_PAGE) {
  const pages = Math.max(1, Math.ceil(total / perPage));
  const pg = Math.min(Math.max(requestedPage, 0), pages - 1);
  return {pg, pages, start: pg * perPage, end: pg * perPage + perPage};
}

export interface ExecutionFacts {
  installation_id?: string;
  discovered?: boolean;
  execution_verified?: boolean;
}

function asFacts(value: unknown): ExecutionFacts | undefined {
  if (!value || typeof value !== 'object') return undefined;
  return value as ExecutionFacts;
}

/** Stored execution facts for one installation, from the executor capability row. */
export function executionFactsFor(
  capabilities: Array<{capability?: string; detail?: Record<string, unknown>}> | undefined,
  installationId: string,
): ExecutionFacts | undefined {
  for (const row of capabilities || []) {
    if (row.capability !== 'executor') continue;
    const evidence = row.detail?.executor_evidence;
    if (!Array.isArray(evidence)) continue;
    const hit = evidence.find((item) => asFacts(item)?.installation_id === installationId);
    const facts = asFacts(hit);
    if (facts) return facts;
  }
  return undefined;
}

/**
 * Label tone for an executor the control plane would admit (state "ready").
 * "ready" only when that selected installation's execution_verified is true.
 * A version probe (discovered) without that receipt is discovered_not_verified.
 * Other capabilities, and executors that are not admitted, return null.
 */
export function executorSetupTone(row: {capability?: string; state?: string; detail?: Record<string, unknown>}): 'ready' | 'discovered_not_verified' | null {
  if (row.capability !== 'executor' || row.state !== 'ready') return null;
  const selected = asFacts(row.detail?.selected);
  const evidence = row.detail?.executor_evidence;
  const facts = Array.isArray(evidence)
    ? asFacts(evidence.find((item) => asFacts(item)?.installation_id === selected?.installation_id))
    : undefined;
  return facts?.execution_verified === true ? 'ready' : 'discovered_not_verified';
}

/**
 * Per-installation Setup card. execution_verified is the only ready tone.
 * discovered (or a version probe when facts were not projected) is not ready.
 */
export function installationSetupTone(
  facts: ExecutionFacts | undefined,
  probe: {available?: boolean} | undefined,
): 'ready' | 'discovered_not_verified' | null {
  if (facts?.execution_verified === true) return 'ready';
  const discovered = facts ? facts.discovered === true : probe?.available === true;
  return discovered ? 'discovered_not_verified' : null;
}
