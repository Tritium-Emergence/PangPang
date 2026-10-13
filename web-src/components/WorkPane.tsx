import {useEffect, useRef, useState, useLayoutEffect} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import type {Routine, Task, Work, Artifact} from '../types';
import {api} from '../api';
import {RunPreview} from './RunPreview';
import {ApprovalButtons} from './ApprovalButtons';
import {activitySummary, pageWindow} from '../pure';
import {attentionLabel, taskLabel, exchangeLabel, dateTime, scheduleText, friendlyError, fileSize, checkpointView} from '../display';
import {MediaPreview} from './MediaPreview';
import {Facts, Created, Value} from './Facts';
import {Modal} from './Modal';

// Work pane. Mirrors the containers the studio tests anchor on: #work-view with
// #projects-grid / #routines / #work-grid / #recovery / #tasks / #artifacts. Attention
// labels for work come from work.attention (server,) — never derived here.

// Viewport-anchored pager: record the scroll-parent-relative top of the
// grid before the page change, restore it after layout so the section does not jump.
// The clamp + slice-bounds math lives in the pure pageWindow() helper (Layer-B locked).
function usePager<T>(items: T[], rootRef: React.RefObject<HTMLElement>) {
  const [page, setPage] = useState(0);
  const {pg, pages, start, end} = pageWindow(items.length, page);
  const anchor = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (anchor.current == null || !rootRef.current) return;
    const after = rootRef.current.getBoundingClientRect().top;
    const sp = scrollParent(rootRef.current);
    sp.scrollTop += after - anchor.current;
    anchor.current = null;
  });
  const go = (next: number) => {
    if (rootRef.current) anchor.current = rootRef.current.getBoundingClientRect().top;
    setPage(next);
  };
  return {pg, pages, total: items.length, slice: items.slice(start, end), go};
}

function scrollParent(node: HTMLElement): HTMLElement {
  for (let n = node.parentElement; n; n = n.parentElement) {
    const o = getComputedStyle(n).overflowY;
    if ((o === 'auto' || o === 'scroll') && n.scrollHeight > n.clientHeight) return n;
  }
  return (document.scrollingElement as HTMLElement) || document.documentElement;
}

function Pager({pg, pages, total, go}: {pg: number; pages: number; total: number; go: (n: number) => void}) {
  const {t: tr} = useTranslation();
  if (pages <= 1) return null;
  return (
    <div className="pager col-span-full mt-2 flex items-center justify-center gap-3 text-sm">
      <button className="rounded border border-line px-2 py-1 disabled:opacity-40" disabled={pg === 0} onClick={() => go(pg - 1)}>‹ {tr('workpane.prevPage')}</button>
      <span className="pager-info text-muted">{tr('workpane.pagerInfo', {page: pg + 1, pages, total})}</span>
      <button className="rounded border border-line px-2 py-1 disabled:opacity-40" disabled={pg >= pages - 1} onClick={() => go(pg + 1)}>{tr('workpane.nextPage')} ›</button>
    </div>
);
}

function useArtifactPager(
  rootRef: React.RefObject<HTMLElement>,
  refreshKey: string,
  onError: (message: string) => void,
) {
  const [items, setItems] = useState<Artifact[]>([]);
  const [page, setPage] = useState(0);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const anchor = useRef<number | null>(null);
  const requestSeq = useRef(0);

  const load = async (index: number, cursor: string | null) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      const q = cursor ? '&cursor=' + encodeURIComponent(cursor): '';
      const result = await api<{items: Artifact[]; next_cursor?: string | null}>(`artifacts?limit=10${q}`);
      if (seq !== requestSeq.current) return; // stale refresh/page response
      setItems(result.items || []);
      setNextCursor(result.next_cursor || null);
      setPage(index);
    } catch (error: any) {
      if (seq !== requestSeq.current) return;
      // Report through friendlyError like every other call site; this was the only
      // place that surfaced a raw server code. A failed page read deliberately mutates
      // NOTHING else: items/nextCursor/page keep their last good values, so the reader
      // stays exactly where they were. Resetting to page 1 here would yank someone
      // browsing deep history back to the top on a single transient failure.
      onError(friendlyError(error));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    setCursors([null]);
    void load(0, null);
    // A new newest Artifact invalidates the first-page cursor chain. Other snapshot
    // refreshes do not reset a user who is browsing older history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  useLayoutEffect(() => {
    if (anchor.current == null || !rootRef.current) return;
    const after = rootRef.current.getBoundingClientRect().top;
    const sp = scrollParent(rootRef.current);
    sp.scrollTop += after - anchor.current;
    anchor.current = null;
  });

  const go = async (next: number) => {
    if (next < 0) return;
    let cursor = cursors[next];
    if (next === page + 1) {
      if (!nextCursor) return;
      cursor = nextCursor;
      setCursors((prev) => {
        const copy = prev.slice(0, next);
        copy[next] = nextCursor;
        return copy;
      });
    }
    if (cursor === undefined) return;
    if (rootRef.current) anchor.current = rootRef.current.getBoundingClientRect().top;
    await load(next, cursor);
  };

  return {items, page, nextCursor, loading, go};
}

function ArtifactPager({page, nextCursor, loading, go}: {
  page: number;
  nextCursor: string | null;
  loading: boolean;
  go: (n: number) => Promise<void>;
}) {
  const {t: tr} = useTranslation();
  if (page === 0 && !nextCursor) return null;
  return (
    <div className="pager artifact-pager col-span-full mt-2 flex items-center justify-center gap-3 text-sm">
      <button className="rounded border border-line px-2 py-1 disabled:opacity-40" disabled={page === 0 || loading} onClick={() => void go(page - 1)}>‹ {tr('workpane.prevPage')}</button>
      <span className="pager-info text-muted">{tr('workpane.artifactPageInfo', {page: page + 1})}{loading ? ` · ${tr('workpane.loading')}`: ''}</span>
      <button className="rounded border border-line px-2 py-1 disabled:opacity-40" disabled={!nextCursor || loading} onClick={() => void go(page + 1)}>{tr('workpane.nextPage')} ›</button>
    </div>
);
}

export function WorkPane({store}: {store: Store}) {
  const {t: tr} = useTranslation();
  const snap = store.snapshot!;
  const workGridRef = useRef<HTMLDivElement>(null);
  const routineRef = useRef<HTMLDivElement>(null), taskRef = useRef<HTMLDivElement>(null), artifactRef = useRef<HTMLDivElement>(null);
  const [detail, setDetail] = useState<Work | null>(null);

  const works = [...(snap.work || [])].reverse();
  // Group once: the project cards below need per-project Works twice each, and scanning
  // the whole (up to 1000-row) window per card made the grid O(projects x works) on
  // every snapshot refresh.
  const projectWork = new Map<string, Work[]>();
  for (const w of works) if (w.project_id) (projectWork.get(w.project_id) || projectWork.set(w.project_id, []).get(w.project_id)!).push(w);
  const wp = usePager<Work>(works, workGridRef);
  const unknownTasks = (snap.tasks || []).filter((t) => t.status === 'unknown');
  const tasks = [...(snap.tasks || [])].reverse();
  const rp = usePager<Routine>([...(snap.routines || [])].reverse(), routineRef);
  const tp = usePager<Task>(tasks, taskRef);
  const latestArtifactId = (snap.artifacts || [])[snap.artifacts.length - 1]?.id || '';
  const ap = useArtifactPager(artifactRef, latestArtifactId, (message) => store.setNotice(message));
  const botName = (id?: string) => snap.bots.find((b) => b.id === id)?.name || id || '';

  return (
    <section id="work-view" className="scroll-none flex-1 overflow-auto p-5">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold tracking-widest text-muted">{tr('workpane.projectsOverview')}</h3>
        <button id="new-project" className="rounded-lg border border-line px-3 py-1 text-xs" onClick={async () => {
          const title = prompt(tr('workpane.projectNamePrompt'))?.trim();
          if (!title) return;
          try { await api('projects', {bot_id: store.selected, title}); await store.refresh(); }
          catch (e: any) { store.setNotice(e?.message || tr('workpane.createProjectFailed')); }
        }}>＋ {tr('workpane.newProject')}</button>
      </div>
      <div id="projects-grid" className="mt-2 grid grid-cols-2 gap-3">
        {(snap.projects || []).length === 0 ? <div className="panel col-span-full rounded-xl border border-line bg-raised p-4 text-sm text-muted">{tr('workpane.noProjects')}</div>: null}
        {[...(snap.projects || [])].reverse().map((p) => (
          <div key={p.id} className="panel rounded-xl border border-line bg-raised p-4">
            <h4 className="font-medium">{p.title}</h4>
            <p className="text-xs text-muted">{botName(p.owner_bot_id)} · {tr('workpane.projectWorkCount', {n: projectWork.get(p.id)?.length || 0})}</p>
            {p.goal ? <p className="mt-1 text-sm">{p.goal}</p>: null}
            <p className="text-xs text-muted">{tr('workpane.projectAttentionCount', {n: (projectWork.get(p.id) || []).filter((w) => ['needs_attention', 'blocked', 'working', 'unknown'].includes(w.attention || 'unknown')).length})}</p>
          </div>
))}
      </div>

      <h3 className="mt-6 text-sm font-semibold tracking-widest text-muted">{tr('workpane.routines')}</h3>
      <div id="routines" ref={routineRef} className="mt-2 grid grid-cols-2 gap-3">
        {(snap.routines || []).length === 0 ? (
          <div className="panel col-span-full rounded-xl border border-line bg-raised p-4 text-sm text-muted">{tr('workpane.noRoutines')}</div>
): rp.slice.map((r) => <RoutineCard key={r.id} r={r} store={store} />)}
        <Pager {...rp} />
      </div>

      <h3 className="mt-6 text-sm font-semibold tracking-widest text-muted">{tr('workpane.worksInProgress')}</h3>
      <div id="work-grid" ref={workGridRef} className="mt-2 grid grid-cols-2 gap-3">
        {wp.slice.map((w) => (
          <div key={w.id} className="panel rounded-xl border border-line bg-raised p-4">
            <div className="flex items-center justify-between">
              <h4 className="font-medium">{botName(w.owner_bot_id)}</h4>
              <span className="text-xs text-muted" data-attn={w.attention}>{tr(attentionLabel[w.attention || 'unknown'] ?? 'attention.unknown')}</span>
            </div>
            <p className="mt-1 line-clamp-2 text-sm">{w.goal}</p>
            <Created at={w.created} />
            <Facts value={checkpointView(w.checkpoint, tr)} labels={checkpointLabels(tr)} />
            <div className="mt-2 flex flex-wrap gap-3">
            <button className="text-xs text-brand" onClick={() => setDetail(w)}>{tr('workpane.viewDetail')}</button>
            <button className="text-xs text-brand" onClick={() => { store.select(w.owner_bot_id); (window as any).__gotoChat?.(); }}>{tr('workpane.openBot')}</button>
            {w.kind === 'conversation' && !w.parent_work_id ? (
              <button className="mt-2 text-xs text-brand hover:underline" onClick={() => { store.select(w.owner_bot_id); store.setContinueTarget({id: w.id, goal: w.goal || ''}); (window as any).__gotoChat?.(); }}>{tr('workpane.continueWork')}</button>
): null}
            </div>
          </div>
))}
        <Pager {...wp} />
      </div>

      <h3 className="mt-6 text-sm font-semibold tracking-widest text-muted">{tr('workpane.executionTasks')}</h3>
      <div id="recovery" className="mt-2 grid grid-cols-2 gap-3">
        {unknownTasks.length === 0 ? <div className="panel col-span-full rounded-xl border border-line bg-raised p-4 text-sm text-muted">{tr('workpane.noUnknownTasks')}</div>: null}
        {unknownTasks.map((t) => (
          <div key={t.id} className="panel task rounded-xl border border-warn/40 bg-warn/5 p-4" data-task={t.id}>
            <div className="text-xs font-medium text-warn">{t.installation_id} · {t.host_id} · {tr('workpane.statusUnknown')}</div>
            <p className="mt-1 text-sm text-muted">{tr('workpane.unknownTaskNote')}</p>
            <p className="text-xs text-warn">{tr('workpane.workspaceIsolated')}</p>
            <RecoveryActions task={t} store={store} />
          </div>
))}
      </div>
      <div id="tasks" ref={taskRef} className="mt-2 grid grid-cols-2 gap-3">
        {(snap.claims || []).map((c) => (
          <div key={c.id} className="panel task rounded-xl border border-warn/40 bg-warn/5 p-4">
            <div className="text-xs font-medium text-warn">{tr('workpane.claimPending')} · {c.key}</div>
            <p className="mt-1 text-sm text-muted">{tr('workpane.claimNote')}</p>
            <div className="mt-2">
              <Act label={tr('workpane.submitAndRelease')} store={store} run={async () => {
                const evidence = prompt(tr('workpane.claimEvidencePrompt'))?.trim();
                if (!evidence || !confirm(tr('workpane.claimConfirm'))) return;
                await api(`claims/${c.id}/resolve`, {confirmed_stopped: true, evidence});
              }} />
            </div>
          </div>
))}
        {tp.slice.map((t) => <TaskPanel key={t.id} t={t} store={store} />)}
        <Pager {...tp} />
      </div>

      <h3 className="mt-6 text-sm font-semibold tracking-widest text-muted">{tr('workpane.artifacts')}</h3>
      <div id="artifacts" ref={artifactRef} className="mt-2 grid grid-cols-2 gap-3">
        {ap.items.map((a: Artifact) => (
          <div key={a.id} data-artifact={a.id} className="panel artifact-card rounded-xl border border-line bg-raised p-4">
            <h4 className="truncate font-medium">{a.path.split(/[\\/]/).pop()}</h4>
            <p className="text-xs text-muted">{a.host_id} · {fileSize(a.size)}</p>
            <MediaPreview artifact={a} />
            <div className="mt-2 flex flex-wrap gap-2">
              <button className="rounded-lg border border-line px-2 py-1 text-xs" onClick={() => {
                store.setAttached((prev) => prev.some((x) => x.id === a.id) ? prev: [...prev, {id: a.id, name: a.path.split(/[\\/]/).pop() || a.id}]);
                (window as any).__gotoChat?.();
              }}>{tr('workpane.referenceInChat')}</button>
              <Act ghost label={tr('workpane.copyToHost')} store={store} run={async () => {
                const host = prompt(tr('workpane.targetHostPrompt'))?.trim();
                if (!host) return;
                const path = prompt(tr('workpane.targetPathPrompt'))?.trim();
                if (!path) return;
                await api(`artifacts/${a.id}/copy`, {host_id: host, path});
              }} />
            </div>
          </div>
))}
        <ArtifactPager page={ap.page} nextCursor={ap.nextCursor} loading={ap.loading} go={ap.go} />
      </div>
      <p className="mt-3 text-xs text-muted">{tr('workpane.snapshotNote')}</p>
      {detail ? <WorkDetail work={snap.work.find((w) => w.id === detail.id) || detail} store={store} onClose={() => setDetail(null)} />: null}
    </section>
);
}

const checkpointLabels = (tr: (k: string) => string) => ({phase: tr('work.checkpoint.phase'), summary: tr('work.checkpoint.summary'), completed: tr('work.checkpoint.completed'), next_actions: tr('work.checkpoint.next_actions'), blocked_on: tr('work.checkpoint.blocked_on')});
function WorkDetail({work, store, onClose}: {work: Work; store: Store; onClose: () => void}) {
  const {t: tr} = useTranslation();
  const snap = store.snapshot!;
  const related = snap.exchanges.filter((x) => [x.work_id, x.source_work_id, x.target_work_id].includes(work.id));
  const name = (id?: string) => snap.bots.find((b) => b.id === id)?.name || id || '?';
  return <Modal id="work-detail-dialog" title={tr('workpane.workDetail')} onClose={onClose}>
    <h2 className="font-semibold">{tr('workpane.workDetail')} · {name(work.owner_bot_id)}</h2>
    <p className="text-xs text-muted">{tr(attentionLabel[work.attention || 'unknown'] ?? 'attention.unknown')}</p>
    <Created at={work.created} />
    <h3 className="mt-3 text-sm font-semibold">{tr('workpane.fullRequirement')}</h3><p className="text-sm">{work.goal || tr('workpane.none')}</p>
    <Facts value={checkpointView(work.checkpoint, tr)} labels={checkpointLabels(tr)} />
    {related.length ? <section><h3 className="mt-3 text-sm font-semibold">{tr('workpane.botMessages')}</h3>{related.map((ex) => <div key={ex.id} className="mt-2 rounded border border-line p-2 text-sm"><p>{name(ex.source_bot_id)} → {name(ex.target_bot_id)} · {ex.status && exchangeLabel[ex.status] ? tr(exchangeLabel[ex.status]): ex.status}</p><p>{ex.summary || ex.request}</p><details><summary>{tr('workpane.exchangeDetails')}</summary><Value value={ex} /></details></div>)}</section>: null}
    <button id="work-detail-close" type="button" className="mt-4 rounded border border-line px-3 py-1" onClick={onClose}>{tr('workpane.close')}</button>
  </Modal>;
}

function TaskPanel({t, store}: {t: Task; store: Store}) {
  const {t: tr} = useTranslation();
  const activity = activitySummary(t, tr);
  return (
    <div className="panel task rounded-xl border border-line bg-raised p-4" data-status={t.status}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium">{taskLabel[t.status] ? tr(taskLabel[t.status]): t.status}</span>
        <span className="text-xs text-muted">{t.installation_id} · {t.host_id}</span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm">{(t.prompt || '').slice(0, 160)}</p>
      <Created at={t.created} />
      {activity ? <p className="mt-1 text-xs text-muted" data-activity-status={t.status}>{activity}</p> : null}
      <details className="mt-2"><summary className="cursor-pointer text-xs text-brand">{tr('workpane.viewTaskAndLogs')}</summary><Value value={{id: t.id, route: t.route, inputs: t.inputs, outputs: t.outputs, receipt: t.receipt, error: t.error}} /></details>
      {t.status === 'unknown' ? <p className="mt-1 text-xs text-warn">{tr('workpane.workspaceIsolatedFull')}</p>: null}
      {t.error ? <p className="mt-1 text-xs text-warn">{t.error}</p>: null}
      <RunPreview task={t} />
      <div className="mt-2 flex flex-wrap gap-2">
        {t.status === 'awaiting_approval' ? <ApprovalButtons task={t} store={store} single={tr('workpane.approveExecution')} /> : null}
        {!['succeeded', 'failed', 'cancelled'].includes(t.status) ? <Act label={tr('workpane.cancel')} run={() => api(`tasks/${t.id}/cancel`, {})} store={store} ghost />: null}
        {['unknown', 'running', 'cancel_requested'].includes(t.status) ? <Act ghost label={tr('workpane.reconcile')} run={() => api(`tasks/${t.id}/reconcile`, {})} store={store} />: null}
        {t.status === 'unknown' ? <RecoveryActions task={t} store={store} />: null}
        {['failed', 'cancelled'].includes(t.status) ? <Act label={tr('workpane.retry')} run={() => api(`tasks/${t.id}/retry`, {})} store={store} />: null}
      </div>
    </div>
);
}

// Routine card. Authority is shown truthfully: grant_write defaults OFF and the UI
// NEVER says "只读". approval sends the exact digest + envelope.
function RoutineCard({r, store}: {r: Routine; store: Store}) {
  const {t: tr} = useTranslation();
  const [grant, setGrant] = useState(false);
  const pending = r.approval_state !== 'approved';
  const derived = store.snapshot!.work.filter((w) => w.routine_id === r.id); // scanned once, read twice
  const authorityText = r.authority?.grant_write === true
    ? tr('workpane.authorityGranted')
: tr('workpane.authorityNotGranted');
  return (
    <div className="panel routine rounded-xl border border-line bg-raised p-4">
      <h4 className="font-medium">{r.owner_policy?.name || r.owner_bot_id}</h4>
      <p className="mt-1 text-sm">{r.instruction}</p>
      <p className="text-xs text-muted">{pending ? tr('workpane.pendingApproval'): r.active ? tr('workpane.pendingTrigger'): r.schedule?.type === 'one-shot' && (r.last_run_at || r.last_manual_run_at) ? tr('workpane.triggered'): tr('workpane.paused')}</p>
      <p className="mt-1 text-xs text-muted">{scheduleText(r, tr)}</p>
      <p className="text-xs text-muted">{tr('workpane.nextRun')}{dateTime(r.next_run_at)} · {tr('workpane.lastRun')}{dateTime(r.last_run_at || r.last_manual_run_at)}</p>
      <p className="text-xs text-muted">{tr('workpane.derivedWork', {n: derived.length})}{derived.map((w) => tr(attentionLabel[w.attention || 'unknown'] ?? 'attention.unknown')).join('、')}</p>
      {r.authority?.required_host_id ? <p className="text-xs text-muted">{tr('workpane.requiredHost')}{r.authority.required_host_id}</p>: null}
      {r.authority?.required_installation_id ? <p className="text-xs text-muted">{tr('workpane.requiredInstallation')}{r.authority.required_installation_id}</p>: null}
      {pending ? (
        <>
          <p className="mt-1 text-xs text-warn">{tr('workpane.pendingApprovalNote')}</p>
          <p className="text-xs">{tr('workpane.botRole')}{r.owner_policy?.role}</p>
          <p className="text-xs">{tr('workpane.botInstructions')}{r.owner_policy?.instructions}</p>
          <label className="mt-2 flex items-center gap-2 text-xs">
            <input type="checkbox" checked={grant} onChange={(e) => setGrant(e.target.checked)} />
            {tr('workpane.grantWriteLabel')}
          </label>
          <div className="mt-2">
            <Act label={tr('workpane.approveAndEnable')} store={store} run={() => {
              const env: Record<string, unknown> = {};
              for (const k of ['required_host_id', 'required_installation_id'] as const) if (r.authority?.[k]) env[k] = r.authority[k];
              return api(`routines/${r.id}/approve`, {digest: r.approval_digest, authority: {...env, grant_write: grant === true}});
            }} />
          </div>
        </>
): (
        <>
          <p className="mt-1 text-xs text-muted">{authorityText}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Act label={tr('workpane.runNow')} store={store} run={() => api(`routines/${r.id}/run`, {})} />
            <Act ghost label={r.active ? tr('workpane.pause'): tr('workpane.resume')} store={store} run={() => api(`routines/${r.id}/${r.active ? 'pause': 'resume'}`, {})} />
          </div>
        </>
)}
    </div>
);
}

function Act({label, run, store, ghost}: {label: string; run: () => Promise<unknown>; store: Store; ghost?: boolean}) {
  const {t: tr} = useTranslation();
  return (
    <button
      className={`rounded-lg px-3 py-1.5 text-sm ${ghost ? 'border border-line text-muted': 'bg-brand text-white'}`}
      onClick={async (e) => {
        const b = e.currentTarget; b.disabled = true;
        try { await run(); store.setNotice(null); await store.refresh(); }
        catch (err: any) { store.setNotice(err?.message || tr('workpane.actionFailed')); }
        finally { b.disabled = false; }
      }}
    >{label}</button>
);
}

export function RecoveryActions({task, store}: {task: Task; store: Store}) {
  const {t: tr} = useTranslation();
  return <div className="mt-2 flex flex-wrap gap-2">{(['confirmed_never_started', 'confirmed_stopped'] as const).map((resolution) => <Act key={resolution} ghost label={resolution === 'confirmed_never_started' ? tr('workpane.confirmNeverStarted'): tr('workpane.confirmStopped')} store={store} run={async () => {
    const evidence = prompt(tr('workpane.recoveryEvidencePrompt'))?.trim();
    if (!evidence || !confirm(resolution === 'confirmed_never_started' ? tr('workpane.recoveryConfirmNeverStarted'): tr('workpane.recoveryConfirmStopped'))) return;
    await api(`tasks/${task.id}/resolve`, {resolution, evidence});
  }} />)}</div>;
}
