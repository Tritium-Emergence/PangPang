import {useCallback, useEffect, useRef, useState} from 'react';
import {api, ApiError} from './api';
import {applyPreviewFrame, mergeRefreshScope} from './pure';
import type {Snapshot, Message} from './types';

// THE single data coordinator: "one root data entry, no second domain
// store"). It owns exactly three things, all server-truth or ephemeral:
// snapshot — the whole dashboard projection (/api/v1/snapshot), read verbatim
// messages — the selected bot's conversation (/api/v1/bots/{id}/messages)
// previews — ephemeral per-turn streaming text from the /bots/{id}/stream SSE
// SSE only INVALIDATES (triggers a refetch) or APPENDS an ephemeral preview; it never
// patches/invents domain truth. Single-flight coalescing mirrors the old app.js so a
// burst of /events during an active turn collapses into one in-flight refresh + one
// replay (not N concurrent snapshot fetches).

export interface Preview {text: string; rev: number; done: boolean}
export interface ContinueTarget {id: string; goal: string}
export interface Attachment {id: string; name: string; preview?: string; uploading?: boolean}

export interface Store {
  snapshot: Snapshot | null;
  messages: Message[];
  previews: Map<string, Preview>; // turn_id -> ephemeral streaming preview
  authed: boolean;
  selected: string;
  // A monotonic selection generation. select() bumps it on EVERY switch, so an A→B→A
  // sequence yields three distinct generations (bot id alone cannot — ABA would alias).
  // Any async op captures (bot, generation) at start via selectionToken(); a late
  // completion may only touch current-Bot ephemeral UI state if captureGeneration still
  // equals the live generation (selectionCurrent()).
  liveSelected: () => string;
  selectionToken: () => number;
  selectionCurrent: (token: number) => boolean;
  select: (botId: string) => void;
  refresh: () => Promise<void>;
  login: (token: string) => Promise<void>;
  logout: () => Promise<void>;
  connection: 'connecting' | 'connected' | 'reconnecting';
  notice: string | null;
  setNotice: (msg: string | null) => void;
  // Ephemeral per-bot UI state (allowed): the one-shot continuation target and pending
  // attachments. Both belong to the bot they were chosen for and are cleared on bot
  // switch (a prior bug). Shared by Composer / WorkPane / Inspector via the store so
  // they never depend on component mount order.
  continueTarget: ContinueTarget | null;
  setContinueTarget: (t: ContinueTarget | null) => void;
  attached: Attachment[];
  setAttached: (fn: (prev: Attachment[]) => Attachment[]) => void;
}

export function useStore(): Store {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [previews, setPreviews] = useState<Map<string, Preview>>(new Map());
  const [authed, setAuthed] = useState(true);
  const [selected, setSelected] = useState('bot-main');
  const [connection, setConnection] = useState<Store['connection']>('connecting');
  const [notice, setNotice] = useState<string | null>(null);
  const [continueTarget, setContinueTarget] = useState<ContinueTarget | null>(null);
  const [attached, setAttached] = useState<Attachment[]>([]);

  // selected bot id as a ref so the SSE effects (mounted once) always read the latest
  // without resubscribing on every selection change.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  // Monotonic selection generation (bumped on every switch) — the true epoch.
  const generation = useRef(0);

  // single-flight: while a refresh runs, mark dirty and replay once on completion.
  const running = useRef(false);
  const dirty = useRef(false);
  const dirtyAll = useRef(false);
  const snapRef = useRef<Snapshot | null>(null);

  // Fetch the selected bot's messages, tagged with the bot id so a late response for a
  // previous bot can never overwrite the current one (bot-switch race). Also prunes
  // previews whose durable final message has landed (retain-until-persisted handoff).
  const loadMessages = useCallback(async (bot: string, snap?: Snapshot) => {
    const msgs = await api<Message[]>(`bots/${bot}/messages`);
    if (selectedRef.current !== bot) return; // selection moved on — drop stale result
    setMessages(msgs);
    const persisted = new Set(msgs.filter((m) => m.kind === 'assistant' && m.turn_id).map((m) => m.turn_id));
    // A preview is only legitimately pending while its turn is still active. Prune it
    // when its durable final has landed OR when the turn is no longer active with no
    // final (failed/blocked/cancelled) — else partial ephemeral text would look
    // permanent (terminal-failure RCB hole). When no fresh snapshot is supplied
    // (bot-switch load), only the persisted-final rule applies.
    const liveTurns = snap
      ? new Set((snap.turns || []).filter((t) => ['queued', 'running', 'starting'].includes(t.status || '')).map((t) => t.id))
: null;
    setPreviews((prev) => {
      let changed = false;
      const n = new Map(prev);
      for (const [turnId] of prev) {
        if (persisted.has(turnId) || (liveTurns && !liveTurns.has(turnId))) { n.delete(turnId); changed = true; }
      }
      return changed ? n: prev;
    });
  }, []);

  const refresh = useCallback(async (scope: 'all' | 'messages' = 'all') => {
    if (running.current) { dirty.current = true; if (scope === 'all') dirtyAll.current = true; return; }
    running.current = true;
    try {
      let all = scope === 'all';
      do {
        dirty.current = false;
        const thisAll = all || dirtyAll.current;
        dirtyAll.current = false;
        all = false;
        if (thisAll) {
          const snap = await api<Snapshot>('snapshot');
          setAuthed(true);
          snapRef.current = snap;
          setSnapshot(snap);
          await loadMessages(selectedRef.current, snap);
        } else {
          await loadMessages(selectedRef.current, snapRef.current ?? undefined);
        }
      } while (dirty.current);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setAuthed(false);
    } finally {
      running.current = false;
    }
  }, [loadMessages]);

  const select = useCallback((botId: string) => {
    if (selectedRef.current === botId) return;
    selectedRef.current = botId; // live bot id (SSE effects read this)
    generation.current += 1; // bump the monotonic epoch (A→B→A yields 3 generations)
    // Clear per-bot ephemeral state in the SAME batch as the selection change so the new
    // bot is never painted with the previous bot's messages/previews/attachments/target
    // even for one frame (they belong to the bot they were chosen for, a prior bug).
    setSelected(botId);
    setMessages([]);
    setPreviews(new Map());
    setContinueTarget(null);
    setAttached(() => []);
  }, []);
  const liveSelected = useCallback(() => selectedRef.current, []);
  const selectionToken = useCallback(() => generation.current, []);
  const selectionCurrent = useCallback((token: number) => generation.current === token, []);

  // After a bot switch, fetch the new bot's messages (select() already cleared the old
  // ephemeral state synchronously). Keyed on `selected`; skips the initial mount (the
  // login/refresh path loads bot-main).
  const firstSel = useRef(true);
  useEffect(() => {
    if (firstSel.current) { firstSel.current = false; return; }
    if (authed) void loadMessages(selected);
  }, [selected, authed, loadMessages]);

  const login = useCallback(async (token: string) => {
    await api('login', {token});
    setAuthed(true);
    await refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    await api('logout', {});
    setAuthed(false);
  }, []);

  // Global events stream: a server tick only invalidates -> one debounced refresh.
  // Mounted once; stable; never reconnects on snapshot renders (SSE continuity).
  useEffect(() => {
    if (!authed) return;
    let timer: number | undefined;
    let pending: 'all' | 'messages' = 'messages';
    const es = new EventSource('/api/v1/events');
    es.onopen = () => setConnection('connected');
    es.onmessage = (ev) => {
      let type: string | undefined;
      try { type = JSON.parse(ev.data)?.type; } catch { type = undefined; }
      pending = mergeRefreshScope(pending, type);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const scope = pending;
        pending = 'messages';
        void refresh(scope);
      }, 200);
    };
    es.onerror = () => setConnection('reconnecting');
    return () => { window.clearTimeout(timer); es.close(); };
  }, [authed, refresh]);

  // Per-bot token delta stream: ephemeral preview only. Re-subscribes when the selected
  // bot changes (a different stream), but NOT on snapshot renders.
  useEffect(() => {
    if (!authed) return;
    const bot = selected;
    let refreshTimer: number | undefined;
    const es = new EventSource('/api/v1/bots/' + bot + '/stream?deltas=1');
    es.onmessage = (e) => {
      let m: any;
      try { m = JSON.parse(e.data); } catch { return; }
      if (!m.turn_id) return;
      if (m.done) {
        // Mark the preview done but KEEP it until the durable final message with the
        // same turn_id arrives (pruned in refresh()). This retain-until-persisted
        // handoff prevents a flash gap where neither preview nor final is shown.
        // Turn completion always re-reads the snapshot: the stream is not authority.
        setPreviews((prev) => {
          const next = applyPreviewFrame(prev.get(m.turn_id), {done: true});
          if (!next) return prev;
          const n = new Map(prev); n.set(m.turn_id, next); return n;
        });
        window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => void refresh('all'), 50);
        return;
      }
      setPreviews((prev) => {
        const cur = prev.get(m.turn_id);
        const next = applyPreviewFrame(cur, m);
        if (!next || next === cur) return prev;
        const n = new Map(prev); n.set(m.turn_id, next); return n;
      });
    };
    es.onerror = () => {};
    return () => { window.clearTimeout(refreshTimer); es.close(); };
  }, [authed, selected, refresh]);

  // initial load
  useEffect(() => { void refresh(); }, [refresh]);

  return {snapshot, messages, previews, authed, selected, liveSelected, selectionToken, selectionCurrent, select, refresh, login, logout, connection, notice, setNotice, continueTarget, setContinueTarget, attached, setAttached};
}
