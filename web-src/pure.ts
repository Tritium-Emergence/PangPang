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

// Clipboard text for one reply: the same unwrapped body the bubble shows, as
// plain text. Structured payloads stay JSON text; nothing here is rendered HTML.
// After a successful copy the control shows a check for this long, then the label returns.
export const COPY_FEEDBACK_MS = 3000;

export function messagePlainText(content: unknown): string {
  const value = unwrapFrame(content);
  if (typeof value === 'string') return value;
  if (value == null) return '';
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try { return JSON.stringify(value); } catch { return ''; }
}

// Rows the main chat timeline renders. setup_result rows (discover,
// declare_adapter, configure, and the other device setup reports) stay
// stored for the model. The timeline omits them, so the steward's written
// reply is what the chat shows. An execution_result already folded into a
// delivered turn is omitted too: a non-interim message shares its turn id,
// or a delivery id has consumed the row.
export interface ChatTimelineMessage {
  kind?: string;
  turn_id?: string;
  interim?: boolean;
  consumed_by_delivery_id?: string;
  consumed_by_turn_id?: string;
}

export function visibleChatMessages<M extends ChatTimelineMessage>(messages: readonly M[]): M[] {
  const completedTurnIds = new Set(messages.filter((a) => a.turn_id && !a.interim).map((a) => a.turn_id));
  return messages.filter((m) => m.kind !== 'setup_result' && (
    m.kind !== 'execution_result' || (!m.consumed_by_delivery_id && !completedTurnIds.has(m.consumed_by_turn_id))
  ));
}

export interface ChatTimelineItem {
  key: string;
  at: number;
  pin?: boolean;
}

// Pinned rows stay after every unpinned row. An awaiting-approval card uses
// this so a later message cannot bury the one-tap approve control.
export function orderChatTimeline<T extends ChatTimelineItem>(items: readonly T[]): T[] {
  return items.map((item, index) => ({item, index})).sort((a, b) => {
    const ap = a.item.pin ? 1 : 0;
    const bp = b.item.pin ? 1 : 0;
    if (ap !== bp) return ap - bp;
    if (a.item.at !== b.item.at) return a.item.at - b.item.at;
    const byKey = a.item.key.localeCompare(b.item.key);
    return byKey || a.index - b.index;
  }).map((row) => row.item);
}

export function splitPinnedTimeline<T extends ChatTimelineItem>(items: readonly T[]): {flow: T[]; pinned: T[]} {
  const ordered = orderChatTimeline(items);
  return {flow: ordered.filter((item) => !item.pin), pinned: ordered.filter((item) => item.pin)};
}

export interface BotListItem {id: string; kind?: string}
export interface ConversationStamp {bot_id?: string; created?: number}

// Sidebar order. The steward is the bot the server marks kind "main" and stays
// first. Everyone else sorts by the newest snapshot turn created time
// (descending). A bot with no finite turn time follows the timed ones, keeping
// the incoming order.
export function orderBots<B extends BotListItem>(bots: readonly B[], turns: readonly ConversationStamp[]): B[] {
  const last = new Map<string, number>();
  for (const turn of turns) {
    const id = turn.bot_id;
    const at = turn.created;
    if (!id || typeof at !== 'number' || !Number.isFinite(at)) continue;
    const prev = last.get(id);
    if (prev === undefined || at > prev) last.set(id, at);
  }
  return bots.map((bot, index) => ({bot, index})).sort((a, b) => {
    const aMain = a.bot.kind === 'main' ? 0 : 1;
    const bMain = b.bot.kind === 'main' ? 0 : 1;
    if (aMain !== bMain) return aMain - bMain;
    const aAt = last.get(a.bot.id);
    const bAt = last.get(b.bot.id);
    const aTimed = aAt !== undefined;
    const bTimed = bAt !== undefined;
    if (aTimed !== bTimed) return aTimed ? -1 : 1;
    if (aTimed && bTimed && aAt !== bAt) return (bAt as number) - (aAt as number);
    return a.index - b.index;
  }).map((row) => row.bot);
}

const MEDIA_EXT: Record<string, string[]> = {
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'],
  video: ['mp4', 'webm', 'mov'],
  audio: ['mp3', 'wav', 'ogg', 'm4a'],
};

// Contract `rel` is the name the user was promised. Split that on `/` only, so a
// POSIX name that contains a backslash stays intact. A stored `name` with no
// contract rel may be a Windows path written by Path.name on Linux; that one
// is a basename on both separators, same as the path fallback.
export function artifactFileName(artifact: {path?: string; rel?: string; name?: string}): string {
  const rel = typeof artifact.rel === 'string' ? artifact.rel : '';
  if (rel && !rel.startsWith('/') && !rel.startsWith('\\') && !rel.split('/').includes('..')) {
    const base = rel.split('/').pop();
    if (base && base !== '.' && base !== '..') return base;
  }
  const stored = typeof artifact.name === 'string' && artifact.name ? artifact.name : (artifact.path || '');
  const base = stored.split(/[\\/]/).filter((part) => part.length > 0).pop();
  return base && base !== '.' && base !== '..' ? base : 'file';
}

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

export type RefreshScope = 'all' | 'messages' | 'snapshot';

export interface RefreshWant {
  snapshot: boolean;
  messages: boolean;
}

// What one refresh call needs. Snapshot-only is the chat progress poll: the
// dashboard projection already carries task activity, so the transcript stays put.
export function refreshWant(scope: RefreshScope): RefreshWant {
  return {
    snapshot: scope === 'all' || scope === 'snapshot',
    messages: scope === 'all' || scope === 'messages',
  };
}

// Collapse a burst that arrives while a refresh is in flight into one follow-up.
export function mergeRefreshWant(pending: RefreshWant, scope: RefreshScope): RefreshWant {
  const next = refreshWant(scope);
  return {
    snapshot: pending.snapshot || next.snapshot,
    messages: pending.messages || next.messages,
  };
}

export const PER_PAGE = 10;

export interface ActivitySummaryTask {
  status?: string;
  created?: number;
  started?: number;
  required_outputs?: unknown;
  reachability?: {phase?: string; last_log_at?: number | null} | null;
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

const REACH_HIDDEN = new Set(['succeeded', 'failed', 'cancelled', 'cancel_requested']);

// Server phase, ignored once status copy wins (terminal or cancel_requested).
export function displayedReachability(task: {status?: string; reachability?: {phase?: string} | null} | null | undefined): 'unreachable' | 'dispatched' | 'active' | null {
  if (!task || REACH_HIDDEN.has(String(task.status || ''))) return null;
  const phase = task.reachability?.phase;
  if (phase === 'unreachable' || phase === 'dispatched' || phase === 'active') return phase;
  return null;
}

export function progressPhaseText(phase: string | null | undefined, tr: Tr): string | null {
  if (phase === 'unreachable') return tr('progress.deviceUnreachable');
  if (phase === 'dispatched') return tr('progress.dispatchedWaiting');
  return null;
}

// Bot-level awaiting_approval rows. Not derived from the live preview or from
// the current conversation's activity.tasks.
export function botAwaitingApproval<T extends {status?: string; work_id?: string}>(
  tasks: readonly T[],
  ownedWorkIds: ReadonlySet<string>,
): T[] {
  return tasks.filter((task) => task.status === 'awaiting_approval' && ownedWorkIds.has(task.work_id || ''));
}

export function outOfScopeApprovals<T extends {work_id?: string}>(
  awaiting: readonly T[],
  scope: ReadonlySet<string>,
): T[] {
  return awaiting.filter((task) => !scope.has(task.work_id || ''));
}

export function pendingApprovalHint(count: number, tr: Tr): string | null {
  if (!Number.isInteger(count) || count <= 0) return null;
  return tr('chat.pendingApprovalHint', {n: count});
}

const TASK_TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

export interface ChatStatusInput {
  streaming: boolean;
  thinking: boolean;
  recovering: boolean;
  delegate: {name: string; text: string} | null;
  task?: {
    status?: string;
    installation_id?: string;
    prompt?: string;
    reachability?: {phase?: string} | null;
  } | null;
  awaitingResult: boolean;
}

// One chat status sentence. An offline starting/running connector wins over
// thinking, delegation, and “in progress”. cancel_requested and a terminal
// status win over the dispatched “进行中” fallback.
export function chatStatusSentence(input: ChatStatusInput, tr: Tr): string {
  if (input.streaming) return '';
  const task = input.task;
  if (displayedReachability(task) === 'unreachable') return tr('progress.deviceUnreachable');
  const status = String(task?.status || '');
  if (status === 'cancel_requested') return tr('task.cancel_requested');
  if (TASK_TERMINAL.has(status)) return tr('task.' + status);
  if (input.thinking) return tr('chat.thinking');
  if (input.recovering) return tr('chat.workingComplex');
  if (input.delegate) return tr('chat.delegatedTo', {name: input.delegate.name, text: (input.delegate.text || '').slice(0, 40)});
  if (displayedReachability(task) === 'dispatched') return tr('progress.dispatchedWaiting');
  if (task) return tr('chat.dispatchedTo', {
    executor: task.installation_id || tr('chat.executorFallback'),
    prompt: (task.prompt || '').slice(0, 40),
  });
  if (input.awaitingResult) return tr('chat.awaitingResult');
  return '';
}

// Absolute queue or start clock, last log time, latest step, and required files
// already present. The snapshot activity has no age_s; that field stays on the planner packet.
// starting uses its own clock — started is recorded before the process is running.
// A connector reachability phase replaces that clock when there is no real report
// or the device is offline. Terminal and cancel_requested keep their own copy.
export function activitySummary(task: ActivitySummaryTask | null | undefined, tr: Tr): string | null {
  const phaseText = progressPhaseText(displayedReachability(task), tr);
  if (phaseText) return phaseText;
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

// Closed set mirrored from pangpang/activity.py. Names outside it are not shown.
const CHAT_PROGRESS_TOOLS = new Set(['read', 'write', 'edit', 'delete', 'move', 'search', 'execute', 'fetch', 'list']);

// How often Chat re-reads the snapshot while its scope has active work.
// Inside the 5–10s bound: long enough to avoid a request storm, short enough
// that a growing run log shows up without an executor status change.
export const CHAT_PROGRESS_REFRESH_MS = 8000;

export const CHAT_PROGRESS_PATH_MAX = 40;

export interface ChatProgressTask {
  id: string;
  status?: string;
  started?: number;
  created?: number;
  reachability?: {phase?: string} | null;
  activity?: {
    last_log_at?: number | null;
    recent?: {tool?: string; path?: string}[];
    present_outputs?: {path?: string; present?: boolean}[];
  } | null;
}

function finiteClock(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// started when the task has actually started; otherwise created. Newest first.
function progressAnchor(task: {started?: number; created?: number}): number {
  const started = finiteClock(task.started);
  if (started !== null && started > 0) return started;
  const created = finiteClock(task.created);
  if (created !== null && created > 0) return created;
  return 0;
}

// One primary task for the chat status sentence and denseline. The order is
// started/created, then id, so two snapshots with the same clocks name the same task.
// Snapshot rows arrive oldest-first; this pick does not follow that array order.
export function pickChatProgressTask<T extends {id: string; started?: number; created?: number}>(tasks: readonly T[]): {task: T; extra: number} | null {
  if (!tasks.length) return null;
  const ranked = tasks.map((task, index) => ({task, index})).sort((a, b) => {
    const delta = progressAnchor(b.task) - progressAnchor(a.task);
    if (delta !== 0) return delta;
    if (a.task.id !== b.task.id) return a.task.id < b.task.id ? -1 : 1;
    return a.index - b.index;
  });
  return {task: ranked[0].task, extra: tasks.length - 1};
}

export function truncateChatProgressPath(path: string, max = CHAT_PROGRESS_PATH_MAX): string {
  if (!path || /[\u0000-\u001f\u007f]/.test(path)) return '';
  const text = path.split('\\').join('/');
  if (text.length <= max) return text;
  return '…' + text.slice(-(max - 1));
}

// Delivered required outputs already on the snapshot. Absent files are not
// progress. Paths use the same truncation as a tool step. No new poll.
function deliveredOutputLabel(activity: {present_outputs?: {path?: string; present?: boolean}[]}, tr: Tr): string | null {
  const outputs = Array.isArray(activity.present_outputs) ? activity.present_outputs : [];
  const files: string[] = [];
  for (const item of outputs) {
    if (!item || item.present !== true || typeof item.path !== 'string') continue;
    const path = truncateChatProgressPath(item.path);
    if (path) files.push(path);
  }
  if (!files.length) return null;
  return tr('workpane.activityPresent', {files: files.join(', ')});
}

// Narrow chat line: last log time, latest closed-set step, and ·+N when the
// scope has more than one active task. A running task whose recent array is
// empty adds a static waiting phrase and no tool name, unless required
// outputs are already present: those names replace the waiting phrase.
// Missing activity, or activity with no recent array and no clock, returns
// null so the caller keeps the coarse progress sentence. This is not the
// WorkPane summary.
export function chatActivityLine(tasks: readonly ChatProgressTask[], tr: Tr): string | null {
  const picked = pickChatProgressTask(tasks);
  if (!picked?.task.activity) return null;
  const phase = displayedReachability(picked.task);
  // The coarse sentence already says unreachable or dispatched. An empty
  // connector report must not add "waiting for the first tool".
  if (phase === 'unreachable' || phase === 'dispatched') return null;
  const activity = picked.task.activity;
  const parts: string[] = [];
  if (typeof activity.last_log_at === 'number' && activity.last_log_at > 0) {
    parts.push(tr('chat.progressLastLog', {time: clockHMS(activity.last_log_at)}));
  }
  const recent = Array.isArray(activity.recent) ? activity.recent : [];
  for (let i = recent.length - 1; i >= 0; i--) {
    const step = recent[i];
    const tool = step && typeof step.tool === 'string' ? step.tool : '';
    if (!CHAT_PROGRESS_TOOLS.has(tool)) continue;
    const path = typeof step.path === 'string' ? truncateChatProgressPath(step.path) : '';
    parts.push(path ? tr('chat.progressStep', {tool, path}) : tr('chat.progressTool', {tool}));
    break;
  }
  // Local projection always sends recent as an array. Empty means the run log
  // has no complete tool row yet (startup banner only). A missing recent is a
  // different activity shape and stays on the clock or the coarse sentence.
  if (phase !== 'active' && Array.isArray(activity.recent) && activity.recent.length === 0 && picked.task.status === 'running') {
    const delivered = deliveredOutputLabel(activity, tr);
    parts.push(delivered || tr('chat.progressAwaitingTool'));
  }
  if (!parts.length) return null;
  const body = parts.join(' · ');
  return picked.extra > 0 ? body + tr('chat.progressMore', {n: picked.extra}) : body;
}

// True only while this chat has working or active tasks and the page is visible.
export function chatProgressRefreshDue(input: {
  visible: boolean;
  hasProgressWork: boolean;
  activeTaskCount: number;
}): boolean {
  return input.visible && (input.hasProgressWork || input.activeTaskCount > 0);
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
 * "ready" only when that installation is still discovered AND execution_verified.
 * A version probe without that receipt is discovered_not_verified.
 * Other capabilities, and executors that are not admitted, return null.
 */
export function executorSetupTone(row: {capability?: string; state?: string; detail?: Record<string, unknown>}): 'ready' | 'discovered_not_verified' | null {
  if (row.capability !== 'executor' || row.state !== 'ready') return null;
  const selected = asFacts(row.detail?.selected);
  const evidence = row.detail?.executor_evidence;
  const facts = Array.isArray(evidence)
    ? asFacts(evidence.find((item) => asFacts(item)?.installation_id === selected?.installation_id))
    : undefined;
  return facts?.execution_verified === true && facts?.discovered === true ? 'ready' : 'discovered_not_verified';
}

/**
 * Per-installation card. Ready only when a fenced receipt exists and the
 * executor is currently discovered. A probe row is that current check; without
 * one, the stored discovered flag is the same fact. A past receipt does not
 * turn an unavailable or missing probe green.
 */
export function installationSetupTone(
  facts: ExecutionFacts | undefined,
  probe: {available?: boolean} | undefined,
): 'ready' | 'discovered_not_verified' | null {
  const discovered = probe ? probe.available === true : facts?.discovered === true;
  if (!discovered) return null;
  return facts?.execution_verified === true ? 'ready' : 'discovered_not_verified';
}

// A user bubble shown before the durable message exists. knownIds are the
// message ids already on screen when Send was tapped, so an older copy of the
// same text cannot retire the bubble.
export interface PendingSend {
  id: string;
  botId: string;
  text: string;
  created: number;
  knownIds: readonly string[];
}

interface PendingMatch {
  id: string;
  from?: string;
  kind?: string;
  content: unknown;
  created?: number;
}

// Drop a pending bubble once a new durable user message carries the same text.
// Oldest pending consumes the oldest matching message, one each. The consumed
// id stays on the remaining pendings so a later pass cannot spend it again.
export function reconcilePendingSends<M extends PendingMatch>(
  pending: readonly PendingSend[],
  messages: readonly M[],
): PendingSend[] {
  const fresh = messages.filter((m) => m.from === 'user' && m.kind === 'user' && m.id);
  const used = new Set<string>();
  const ordered = [...pending].sort((a, b) => a.created - b.created || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const drop = new Set<string>();
  for (const p of ordered) {
    const known = new Set(p.knownIds);
    const want = p.text.trim();
    const match = fresh.find((m) => {
      if (used.has(m.id) || known.has(m.id)) return false;
      if (typeof m.created === 'number' && m.created + 30 < p.created) return false;
      return (typeof m.content === 'string' ? m.content.trim() : '') === want;
    });
    if (match) { used.add(match.id); drop.add(p.id); }
  }
  if (!drop.size) return pending.slice();
  return pending.filter((p) => !drop.has(p.id)).map((p) => {
    let changed = false;
    const knownIds = p.knownIds.slice();
    const have = new Set(knownIds);
    for (const id of used) if (!have.has(id)) { knownIds.push(id); changed = true; }
    return changed ? {...p, knownIds}: p;
  });
}

// Put a failed send back only when the composer was left empty. Any new text
// or attachment is a newer draft and stays as the user left it.
export function restoreComposerDraft<A extends {id: string}>(
  current: {text: string; attached: readonly A[]},
  sent: {text: string; attached: readonly A[]},
): {text: string; attached: A[]} {
  if (current.text !== '' || current.attached.length > 0) {
    return {text: current.text, attached: current.attached as A[]};
  }
  return {text: sent.text, attached: sent.attached.slice()};
}

export type ViewportAction =
  | {type: 'none'}
  | {type: 'reset'}
  | {type: 'pin'; top: number; height: number};

// How the chat shell should track visualViewport. A full-height pan is a stuck
// scroll (keyboard already closed): the shell cannot follow it and stay inside
// the layout viewport. A shorter visual viewport is the keyboard; pin the shell
// to that rectangle, clamped so it is not clipped.
export function viewportAction(
  vv: {height: number; offsetTop: number} | null | undefined,
  layout: {innerHeight: number} | null | undefined,
): ViewportAction {
  if (!vv || !layout) return {type: 'none'};
  const height = vv.height;
  const rawTop = vv.offsetTop;
  const inner = layout.innerHeight;
  if (!Number.isFinite(height) || !Number.isFinite(rawTop) || !Number.isFinite(inner)) return {type: 'none'};
  if (height < 1 || inner < 1) return {type: 'none'};
  const top = rawTop > 0 ? rawTop : 0;
  if (top < 1 && Math.abs(height - inner) <= 2) return {type: 'none'};
  if (Math.abs(height - inner) <= 2 && top >= 1) return {type: 'reset'};
  const pinH = Math.round(Math.min(height, inner));
  const pinTop = Math.round(Math.min(top, Math.max(0, inner - pinH)));
  if (pinTop < 1 && Math.abs(pinH - inner) <= 2) return {type: 'none'};
  return {type: 'pin', top: pinTop, height: pinH};
}

export function shellFrameCss(frame: {top: number; height: number}): string {
  return `#shell{position:fixed;top:${Math.round(frame.top)}px;left:0;right:0;height:${Math.round(frame.height)}px}`;
}
