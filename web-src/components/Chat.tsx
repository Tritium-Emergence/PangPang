import {useEffect, useLayoutEffect, useRef} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import type {Task} from '../types';
import {MessageBubble} from './MessageBubble';
import {api} from '../api';
import {RunPreview} from './RunPreview';
import {ApprovalButtons} from './ApprovalButtons';
import {clock, dateTime} from '../display';
import {conversationActivity, currentWorkId} from '../scope';

// Chat log. RCB-critical:
// - messages are keyed by stable m.id so React reconciles in place and never rebuilds
// the subtree (scroll stability + node identity).
// - a streaming preview (data-testid=stream-preview) shows only for a turn whose
// durable assistant message has NOT yet landed; once it lands the final bubble
// (data-testid=assistant-msg) renders and the preview is pruned by the store
// (retain-until-persisted hand-off, no flash gap).
// - approval / cancel cards read server status; the client renders request progress
// but never committed reality early (handled in MessageBubble + TaskCard).
export function Chat({store, botId}: {store: Store; botId: string}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // Visible messages: hide execution_result rows already folded into a delivered turn
  // (mirrors the old conversation() visibility rule).
  const completedTurnIds = new Set(store.messages.filter((a) => a.turn_id && !a.interim).map((a) => a.turn_id));
  const visible = store.messages.filter(
    (m) => m.kind !== 'execution_result' || (!m.consumed_by_delivery_id && !completedTurnIds.has(m.consumed_by_turn_id!)),
);

  // Approval cards for this bot's work (inline, chat-ordered). Only tasks still waiting
  // for the user: an approved task's record lives with the task (Inspector / Work), not
  // as a growing pile of history in the conversation.
  const myWorkIds = new Set((snap.work || []).filter((w) => w.owner_bot_id === botId).map((w) => w.id));
  const taskCards = (snap.tasks || []).filter((t) => myWorkIds.has(t.work_id!) && t.status === 'awaiting_approval');

  // Unknown recovery cards (named action, not anonymous spinner).
  const unknownTasks = (snap.tasks || []).filter((t) => t.owner_bot_id === botId && t.status === 'unknown');

  // Streaming previews whose final message is not yet persisted.
  const persistedTurnIds = new Set(store.messages.filter((m) => m.kind === 'assistant' && m.turn_id).map((m) => m.turn_id));
  const livePreviews = [...store.previews.entries()].filter(([turnId, p]) => p.text && !persistedTurnIds.has(turnId));

  // record whether we're pinned to the bottom BEFORE paint so a background refresh
  // that changes nothing keeps the scroll position (React keyed reconcile does the
  // no-rebuild; this only governs auto-follow).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  });

  // Record the newest rendered message id as this tab's last-seen event (per
  // bot×stream), so the next send can establish correction-lineage adjacency on the
  // server (service._stream_context). Mirrors the old app.js setLastSeenEventId.
  useEffect(() => {
    const last = visible[visible.length - 1];
    if (!last?.id) return;
    const sid = sessionStorage.getItem('conversationStreamId:' + botId);
    if (sid) sessionStorage.setItem('lastSeenEventId:' + botId + ':' + sid, last.id);
  });

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => { atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
    el.addEventListener('scroll', onScroll);
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  // The keyboard resizes this scroller without a React render. Stay on the latest
  // line when the reader was already pinned there.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    let height = el.clientHeight;
    const observer = new ResizeObserver(() => {
      const next = el.clientHeight;
      if (next === height) return;
      height = next;
      if (atBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const botName = (id?: string) => snap.bots.find((b) => b.id === id)?.name || id || t('chat.botFallback');
  const pendingSends = store.pendingSends.filter((p) => p.botId === botId);
  const timeline = [
...visible.map((m) => ({key: 'message-' + m.id, at: m.created || 0, node: <MessageBubble m={m} snap={snap} botName={botName} />})),
...taskCards.map((task) => ({key: 'approval-' + task.id, at: task.created || 0, node: <TaskCard task={task} store={store} />})),
...pendingSends.map((p) => ({key: 'pending-' + p.id, at: p.created || 0, node: <div data-testid="pending-msg"><MessageBubble m={{id: p.id, kind: 'user', from: 'user', content: p.text, created: p.created}} snap={snap} botName={botName} /></div>})),
  ].sort((a, b) => a.at - b.at || a.key.localeCompare(b.key));
  const curId = currentWorkId(botId, store.messages);
  const current = snap.work.find((w) => w.id === curId);
  // A tab may still be reading a delivered conversation while this Bot works on
  // another job. This display fallback does not alter Inspector/action scope.
  const progressWork = current?.attention === 'working' ? current: snap.work.filter((w) => w.owner_bot_id === botId && w.attention === 'working').sort((a, b) => (b.created || 0) - (a.created || 0))[0];
  const activity = conversationActivity(snap, botId, progressWork?.id || curId);
  const active = snap.turns.filter((t) => t.bot_id === botId && ['queued', 'running', 'starting'].includes(t.status || ''));
  const exchange = activity.exchange; // scope.ts owns the in-scope predicate
  const delegated = snap.work.find((w) => activity.scope.has(w.id) && w.owner_bot_id !== botId && w.attention === 'working');
  const recovering = snap.turns.some((t) => activity.scope.has(t.work_id || '') && t.recovery_dispatched && activity.tasks.some((task) => task.id === t.recovery_dispatched));
  // Describe durable activity facts. Work badges and attention remain server-owned.
  const delegate = exchange ? {bot: exchange.target_bot_id, text: exchange.request || exchange.summary}: delegated ? {bot: delegated.owner_bot_id, text: delegated.goal}: null;
  const progress = livePreviews.length ? '': active.length ? t('chat.thinking'): recovering ? t('chat.workingComplex'): delegate ? t('chat.delegatedTo', {name: snap.bots.find((b) => b.id === delegate.bot)?.name || delegate.bot, text: (delegate.text || '').slice(0, 40)}): activity.tasks.length ? t('chat.dispatchedTo', {executor: activity.tasks[0].installation_id || t('chat.executorFallback'), prompt: (activity.tasks[0].prompt || '').slice(0, 40)}): progressWork ? t('chat.awaitingResult'): '';

  return (
    <div
      id="messages"
      data-testid="chat-log"
      ref={scrollRef}
      aria-live="polite"
      className="scroll-none flex-1 overflow-auto px-5 py-4"
    >
      {visible.length === 0 && taskCards.length === 0 && unknownTasks.length === 0 && !pendingSends.length && !progress && !livePreviews.length ? (
        <div className="grid h-full place-items-center text-center text-muted">
          <div>
            <h2 className="text-lg font-medium text-ink">{t('chat.emptyTitle')}</h2>
            <p className="mt-1 text-sm">{t('chat.emptyHint', {name: botName(botId)})}</p>
          </div>
        </div>
): null}

      {timeline.map((item) => <div key={item.key} data-timeline={item.key} data-created={item.at}>{item.node}</div>)}

      {unknownTasks.map((t: Task) => (
        <RecoveryCard key={t.id} task={t} snap={snap} store={store} />
))}

      {progress ? <div className="bubble thinking my-2 max-w-[80%] text-sm" role="status">
        {/* One Bot timeline can hold several live Works, so a Work-scoped bubble names its Work. */}
        <div className="source truncate text-xs text-muted">{botName(botId)}{!active.length && progressWork ? ` · ${progressWork.goal}`: ''}</div>
        <div className="body mt-1 rounded-2xl bg-raised px-4 py-2 text-muted shadow-sm">{progress}</div>
      </div>: null}
      {livePreviews.map(([turnId, p]) => (
        <div key={'preview-' + turnId} data-testid="stream-preview" data-turn={turnId} className="my-2 max-w-[80%]">
          <div className="text-xs text-muted">{botName(botId)}</div>
          <div className="body mt-1 whitespace-pre-wrap rounded-2xl bg-raised px-4 py-2 text-ink shadow-sm">{p.text}</div>
        </div>
))}
    </div>
);
}

// Inline approval card for an awaiting task. RCB: it stays "需你确认" until the server
// durably approves; the approve button only renders request progress (disabled while in
// flight). The card leaves the chat only when server truth says the task is no longer
// awaiting — never optimistically.
function TaskCard({task, store}: {task: Task; store: Store}) {
  const {t} = useTranslation();
  return (
    <div className="bubble tcard awaiting my-2 max-w-[80%] rounded-2xl border border-warn/40 bg-warn/5 px-4 py-3">
      {task.created ? <time className="text-xs text-muted" title={dateTime(task.created)}>{clock(task.created)}</time>: null}
      <div className="text-xs font-medium text-muted">{t('chat.awaitingApprovalLabel')}</div>
      <p className="mt-1 text-sm">{t('chat.needsConfirm')}</p>
      <p className="mt-1 text-xs text-muted">{(task.prompt || '').slice(0, 160)}</p>
      <p className="mt-1 text-xs text-muted">{t('chat.executorPrefix', {id: task.installation_id || '—'})}</p>
      <RunPreview task={task} />
      <div className="mt-2 flex gap-2">
        <ApprovalButtons task={task} store={store} single={t('chat.approve')} />
        <ActionButton label={t('chat.cancel')} ghost run={() => api(`tasks/${task.id}/cancel`, {})} store={store} />
      </div>
    </div>
);
}

// Named recovery card for an unknown task. Explicit "停止此工作"
// action; never an anonymous spinner.
function RecoveryCard({task, snap, store}: {task: Task; snap: Store['snapshot']; store: Store}) {
  const {t} = useTranslation();
  const work = (snap!.work || []).find((w) => w.id === task.work_id);
  return (
    <div className="bubble warning my-2 max-w-[80%] rounded-2xl border border-warn/40 bg-warn/5 px-4 py-3" data-task={task.id}>
      <div className="text-xs font-medium text-warn">{t('chat.needsAttention')} · {work?.goal || task.work_id}</div>
      <p className="mt-1 text-sm">{t('chat.unknownTaskNote', {id: task.id})}</p>
      <div className="mt-2 flex gap-2">
        <ActionButton label={t('chat.stopWork')} run={() => api(`work/${task.work_id}/cancel`, {})} store={store} />
      </div>
    </div>
);
}

// A button that renders ONLY request progress: disabled while the POST is in flight,
// then a store refresh reflects the durable server truth. It never writes committed
// state locally (RCB).
function ActionButton({label, run, store, ghost}: {label: string; run: () => Promise<unknown>; store: Store; ghost?: boolean}) {
  const {t} = useTranslation();
  const busy = useRef(false);
  return (
    <button
      className={`rounded-lg px-3 py-1.5 text-sm ${ghost ? 'border border-line text-muted': 'bg-brand text-white'}`}
      onClick={async (e) => {
        const btn = e.currentTarget;
        if (busy.current) return;
        busy.current = true; btn.disabled = true;
        try { await run(); store.setNotice(null); await store.refresh(); }
        catch (err: any) { store.setNotice(err?.message || t('chat.operationFailed')); btn.disabled = false; }
        finally { busy.current = false; }
      }}
    >
      {label}
    </button>
);
}
