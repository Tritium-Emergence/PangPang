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
  required_outputs?: unknown;
  activity?: {
    status?: string;
    age_s?: number;
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

// Duration, last log time, latest step, and required files already present.
// starting stays "starting" — started is recorded before the process is running.
export function activitySummary(task: ActivitySummaryTask | null | undefined, tr: Tr): string | null {
  const activity = task?.activity;
  if (!activity) return null;
  const status = String(activity.status || task?.status || '');
  const age = typeof activity.age_s === 'number' ? activity.age_s : 0;
  const parts: string[] = [];
  if (status === 'queued') parts.push(tr('workpane.activityQueued', {age}));
  else if (status === 'starting') parts.push(tr('workpane.activityStarting', {age}));
  else if (status === 'running') parts.push(tr('workpane.activityRunning', {age}));
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

// Pure pagination math: clamp the requested page into range and compute the
// slice bounds. usePager() consumes this so the grid window + clamp are tested once,
// independent of the React scroll-anchor effect.
export function pageWindow(total: number, requestedPage: number, perPage: number = PER_PAGE) {
  const pages = Math.max(1, Math.ceil(total / perPage));
  const pg = Math.min(Math.max(requestedPage, 0), pages - 1);
  return {pg, pages, start: pg * perPage, end: pg * perPage + perPage};
}
