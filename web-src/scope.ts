import type {Exchange, Snapshot, Task, Turn} from './types';

const TERMINAL = ['delivered', 'failed', 'cancelled', 'linked'];
const TURN_ACTIVE = ['queued', 'running', 'starting'];
const TASK_ACTIVE = ['queued', 'running', 'starting', 'cancel_requested'];

export interface Activity {
  scope: Set<string>; // the recursive current-Work subtree (non-terminal boundary)
  active: Turn[]; // runnable turns in scope (owned by bot)
  tasks: Task[]; // active tasks in scope
  unknown: Task[]; // bot-wide unknown tasks (chat sibling cards)
  exchange?: Exchange; // the pending peer exchange in scope, if any
  waiting: boolean;
}

// Port of the frozen app.js conversationActivity (//). `scope` is the
// recursive current-Work subtree bounded at terminal Works. It is the SINGLE source of
// truth for the Current-Work inspector's action targets: approve / stop /
// recovery act on the SAME subtree the server's recursive attention rollup used — never
// a sibling Work's task. `unknown` stays bot-wide (the chat shows sibling recovery
// cards), but the inspector action scope must use `scope`, not `unknown`.
export function conversationActivity(state: Snapshot, bot: string, workId: string | null | undefined): Activity {
  const owned = new Set((state.work || []).filter((w) => w.owner_bot_id === bot).map((w) => w.id));
  const work = (state.work || []).find((w) => w.id === workId && owned.has(w.id));
  const scope = new Set<string>(work && !TERMINAL.includes(work.status || '') ? [work.id]: []);
  const children = new Map<string | null | undefined, typeof state.work>();
  for (const w of state.work || []) {
    if (!children.has(w.parent_work_id)) children.set(w.parent_work_id, []);
    children.get(w.parent_work_id)!.push(w);
  }
  const pending = [...scope];
  for (let i = 0; i < pending.length; i++) {
    for (const w of children.get(pending[i]) || []) {
      if (!scope.has(w.id) && !TERMINAL.includes(w.status || '')) { scope.add(w.id); pending.push(w.id); }
    }
  }
  const active = (state.turns || []).filter((t) => t.bot_id === bot && scope.has(t.work_id || '') && TURN_ACTIVE.includes(t.status || ''));
  const tasks = (state.tasks || []).filter((t) => scope.has(t.work_id || '') && TASK_ACTIVE.includes(t.status || ''));
  const childActive = (state.turns || []).some((t) => scope.has(t.work_id || '') && t.work_id !== workId && TURN_ACTIVE.includes(t.status || ''));
  // An exchange is in scope if EITHER end is — a delegation's source Work can sit
  // outside the subtree while its target Work is inside. Returned as the row (not a
  // boolean) so the chat's progress bubble reads this one predicate instead of keeping
  // a second copy that short-circuited on source_work_id and disagreed here.
  const exchange = (state.exchanges || []).find((x) => (scope.has(x.source_work_id || '') || scope.has(x.work_id || '')) && x.status === 'pending');
  const unknown = (state.tasks || []).filter((t) => owned.has(t.work_id || '') && t.status === 'unknown');
  return {scope, active, tasks, unknown, exchange, waiting: tasks.length > 0 || childActive || !!exchange};
}

// The current conversation Work for a bot: the explicit sticky hint (sessionStorage),
// else the newest user message's work_id. Mirrors old app.js getCurrentWorkId fallback.
export function currentWorkId(bot: string, messages: {from?: string; work_id?: string}[]): string | null {
  const hint = sessionStorage.getItem('currentWorkId:' + bot);
  if (hint) return hint;
  const lastUser = [...messages].reverse().find((m) => m.from === 'user' && m.work_id);
  return lastUser?.work_id || null;
}
