import {useTranslation} from 'react-i18next';
import type {Snapshot, Work} from '../types';
import type {Store} from '../store';
import {api} from '../api';

// correctness gate: the 待办 inbox renders the server's attention projection
// VERBATIM — it never computes any state client-side. It drops the old count row
// (5 non-actionable numbers) and lists only things the user can act on, each item
// opening the place to act: Works needing a decision / blocked, pending routines,
// and unread results to review. The server owns precedence; the client only renders.
export type Goto = {work: (w: Work) => void; pane: () => void};

const ACT = new Set(['needs_attention', 'blocked']); // the two buckets the user acts on

// The Works to act on. A live parent already rolls its child's state up (server
// projection), and the Inspector acts on the whole subtree, so a child whose parent is
// listed is reached through that parent instead of being listed twice.
function actionable(snap: Snapshot): Work[] {
  const listed = (snap.work || []).filter((w) => ACT.has(w.attention || ''));
  const ids = new Set(listed.map((w) => w.id));
  return listed.filter((w) => !w.parent_work_id || !ids.has(w.parent_work_id));
}

// Unread results/peers waiting to be read (the done_unread bucket). These are
// notifications, not Works: show the loaded ones as items that jump to the Bot and
// mark themselves read. attention.py counts up to 1000 but the snapshot carries the
// most recent 500, so the "更多" footer sends the rest to the Work pane.
const UNREAD_KINDS = new Set(['result', 'peer_message']);
function unreadResults(snap: Snapshot) {
  return (snap.notifications || []).filter((n) => !n.read && UNREAD_KINDS.has(n.kind || '') && n.work_id);
}

export function AttentionPanel({snap, goto, store}: {snap: Snapshot; goto: Goto; store: Store}) {
  const {t} = useTranslation();
  const works = actionable(snap);
  const routines = (snap.routines || []).filter((r) => r.approval_state === 'pending');
  const unread = unreadResults(snap);
  const botName = (id: string) => snap.bots.find((b) => b.id === id)?.name || id;
  const summary = snap.attention_summary;
  // "更多" only counts actionable Works beyond the loaded window — a count whose
  // filter matches what the Work pane can actually show. done_unread is NOT folded
  // in: the server counts it over notifications (some without a work_id, capped at
  // 1000), so a precise diff against our work_id-filtered list would mislead — point
  // users at nothing. Unread beyond the loaded snapshot surfaces via the Bot badges.
  const more = summary ? Math.max(0, Number(summary.needs_attention ?? 0) + Number(summary.blocked ?? 0) - works.length - routines.length): 0;

  const openResult = async (botId: string, nid: string) => {
    store.select(botId);
    (window as any).__gotoChat?.();
    try { await api(`notifications/${nid}/read`, {}); await store.refresh(); } catch { /* best-effort read */ }
  };

  const empty = !works.length && !routines.length && !unread.length && !more;
  return (
    <section id="attention-panel" data-testid="attention-panel" className="border-b border-line">
      <div className="px-4 pb-2 pt-3 text-xs font-semibold tracking-widest text-muted">{t('attention2.title')}</div>
      <div className="max-h-72 overflow-auto px-3 pb-3">
        {works.map((w) => (
          <button
            key={w.id} data-work={w.id} onClick={() => goto.work(w)}
            className="attn-item mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-left hover:border-brand"
          >
            <span className={`text-xs font-medium ${w.attention === 'blocked' ? 'text-warn': 'text-brand'}`}>{w.attention === 'needs_attention' ? t('attention2.needsDecision'): t('attention2.blocked')}</span>
            <span className="text-xs text-muted"> · {botName(w.owner_bot_id)}</span>
            <span className="mt-0.5 block truncate text-sm">{w.goal || t('attention2.noGoal')}</span>
          </button>
))}
        {routines.map((r) => (
          <button
            key={r.id} data-routine={r.id} onClick={goto.pane}
            className="attn-item mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-left hover:border-brand"
          >
            <span className="text-xs font-medium text-warn">{t('attention2.routinePending')}</span>
            <span className="mt-0.5 block truncate text-sm">{r.instruction || r.id}</span>
          </button>
))}
        {unread.map((nf) => (
          <button
            key={nf.id} data-notification={nf.id} onClick={() => nf.bot_id && openResult(nf.bot_id, nf.id)}
            className="attn-item mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-left hover:border-brand"
          >
            <span className="text-xs font-medium text-good">{t('attention2.doneUnread')}</span>
            {nf.bot_id ? <span className="text-xs text-muted"> · {botName(nf.bot_id)}</span>: null}
            <span className="mt-0.5 block truncate text-sm">{nf.title || t('attention2.noGoal')}</span>
          </button>
))}
        {more ? <button className="attn-more mt-2 text-xs text-brand hover:underline" onClick={goto.pane}>{t('attention2.more', {n: more})}</button>: null}
        {empty ? <p className="px-1 pt-1 text-sm text-muted">{t('attention2.empty')}</p>: null}
      </div>
    </section>
);
}
