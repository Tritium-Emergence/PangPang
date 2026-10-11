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

// Pure pagination math: clamp the requested page into range and compute the
// slice bounds. usePager() consumes this so the grid window + clamp are tested once,
// independent of the React scroll-anchor effect.
export function pageWindow(total: number, requestedPage: number, perPage: number = PER_PAGE) {
  const pages = Math.max(1, Math.ceil(total / perPage));
  const pg = Math.min(Math.max(requestedPage, 0), pages - 1);
  return {pg, pages, start: pg * perPage, end: pg * perPage + perPage};
}
