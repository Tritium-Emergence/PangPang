import type {Store} from '../store';
import type {AttentionState, Task} from '../types';
import {useTranslation} from 'react-i18next';
import {conversationActivity, currentWorkId} from '../scope';
import {api} from '../api';
import {RunPreview} from './RunPreview';
import {ApprovalButtons} from './ApprovalButtons';
import {friendlyError} from '../display';
import {RecoveryActions} from './WorkPane';
import {AttentionPanel, type Goto} from './AttentionBar';

// Right inspector: the CURRENT Work and its control/recovery actions. Attention
// labels are read VERBATIM from work.attention (server projection) — never derived. The
// action scope is the recursive current-Work subtree (conversationActivity.scope), so
// Stop / approve / unknown-resolve / reconcile act ONLY on the current Work and its
// descendants — never on a sibling Work's task (the action-scope P1). Above it, the
// 待处理 panel lists what needs the user; picking an item makes it the current Work here.
const ATTN_LABEL: Record<AttentionState, string> = {
  needs_attention: 'inspector.attn.needsAttention', blocked: 'inspector.attn.blocked', working: 'inspector.attn.working',
  done_unread: 'inspector.attn.doneUnread', settled: 'inspector.attn.settled', unknown: 'inspector.attn.unknown',
};
const ATTN_TONE: Record<AttentionState, string> = {
  needs_attention: 'text-warn', blocked: 'text-warn', working: 'text-brand',
  done_unread: 'text-good', settled: 'text-muted', unknown: 'text-muted',
};

export function Inspector({store, botId, goto}: {store: Store; botId?: string; goto: Goto}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  if (!botId) return <aside className="hidden w-72 shrink-0 flex-col border-l border-line bg-raised lg:flex"><AttentionPanel snap={snap} goto={goto} /></aside>;

  const curId = store.continueTarget?.id || currentWorkId(botId, store.messages);
  const activity = conversationActivity(snap, botId, curId);
  const current = (snap.work || []).find((w) => w.id === curId);

  // Action targets are strictly within the current-Work subtree (activity.scope).
  const scopeTasks = (snap.tasks || []).filter((t) => activity.scope.has(t.work_id || ''));
  const awaiting = scopeTasks.filter((t) => t.status === 'awaiting_approval');
  const unknownInScope = scopeTasks.filter((t) => t.status === 'unknown');
  // Work-level Stop truth: "stopping" comes from the current WORK's own
  // cancel_requested (durable), NOT derived from a task's status. Stop is offered for
  // ANY non-terminal Work (terminal = delivered/failed/cancelled/linked/archived),
  // independent of whether an active turn/task exists — so a blocked Work with a failed
  // task and no active task (e.g.) still shows Stop.
  const terminal = ['delivered', 'failed', 'cancelled', 'linked', 'archived'];
  const isTerminal = !!current && terminal.includes(current.status || '');
  // Request-progress never overrides committed terminal reality: cancel_work() may
  // legitimately leave cancel_requested=true on an already-cancelled Work.
  const stopping = !!current?.cancel_requested && !isTerminal;
  const stoppable = !!current && !isTerminal && !stopping;
  // 受阻 says that the Work stopped; the newest blocked turn in this snapshot says why.
  // The snapshot's turn window is bounded, so a missing turn shows no reason, never a guess.
  const blockedTurn = current?.attention === 'blocked'
    ? [...snap.turns].reverse().find((t) => activity.scope.has(t.work_id || '') && t.status === 'blocked' && t.error)
: undefined;

  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); store.setNotice(null); await store.refresh(); }
    catch (e: any) { store.setNotice(e?.message || t('inspector.operationFailed')); }
  };

  return (
    <aside className="hidden w-72 shrink-0 flex-col border-l border-line bg-raised lg:flex">
      <AttentionPanel snap={snap} goto={goto} />
      <div id="work-inspector" data-testid="work-inspector" className="flex min-h-0 flex-1 flex-col">
        <div className="border-b border-line px-4 py-3 text-xs font-semibold tracking-widest text-muted">{t('inspector.currentWork')}</div>
        <div className="flex-1 overflow-auto p-3">
          {!current ? (
            <p className="text-sm text-muted">{t('inspector.noActiveWork')}</p>
): (
            <div className="rounded-xl border border-line bg-surface p-3">
              {/* attention label read verbatim; fail-closed to `unknown` if absent (RCB) */}
              {(() => { const a = (current.attention || 'unknown') as AttentionState;
                return <div className={`text-xs font-medium ${ATTN_TONE[a]}`}>{t(ATTN_LABEL[a])}</div>; })()}
              <p className="mt-1 text-sm">{current.goal || t('inspector.noGoal')}</p>
              {blockedTurn ? <p data-testid="blocked-reason" className="mt-1 text-xs text-warn">{t('inspector.blockedReason')}{friendlyError(blockedTurn.error)}</p>: null}

              {/* Stop: Work-level RCB. "正在取消…" strictly from the WORK's durable
                  cancel_requested; "已取消" never shown optimistically. Stop is available
                  for any non-terminal Work regardless of task activity. */}
              {stopping ? (
                <p className="mt-2 text-xs text-warn">{t('inspector.cancelling')}</p>
): stoppable ? (
                <button className="mt-2 rounded-lg border border-line px-3 py-1 text-xs" onClick={() => act(() => api(`work/${current.id}/cancel`, {}))}>{t('inspector.stopWork')}</button>
): null}

              {awaiting.map((t2: Task) => (
                <div key={t2.id} className="mt-3 rounded-lg border border-warn/40 bg-warn/5 p-2">
                  <p className="text-xs">{t('inspector.needsApproval', {prompt: (t2.prompt || '').slice(0, 80)})}</p>
                  <RunPreview task={t2} />
                  <div className="mt-1 flex gap-2">
                    <ApprovalButtons task={t2} store={store} single={t('inspector.approve')} compact />
                    <button className="rounded border border-line px-2 py-1 text-xs" onClick={() => act(() => api(`tasks/${t2.id}/cancel`, {}))}>{t('inspector.cancel')}</button>
                  </div>
                </div>
))}

              {unknownInScope.map((t2: Task) => (
                <div key={t2.id} className="mt-3 rounded-lg border border-warn/40 bg-warn/5 p-2" data-task={t2.id}>
                  <p className="text-xs text-warn">{t('inspector.unknownStatus', {installation: t2.installation_id, host: t2.host_id})}</p>
                  <div className="mt-1 flex flex-wrap gap-2">
                    <button className="rounded border border-line px-2 py-1 text-xs" onClick={() => act(() => api(`tasks/${t2.id}/reconcile`, {}))}>{t('inspector.reconcile')}</button>
                    <RecoveryActions task={t2} store={store} />
                  </div>
                </div>
))}

              <button className="mt-3 text-xs text-brand hover:underline" onClick={() => { store.setContinueTarget({id: current.id, goal: current.goal || ''}); (window as any).__gotoChat?.(); }}>{t('inspector.continueWork')}</button>
            </div>
)}
        </div>
      </div>
    </aside>
);
}
