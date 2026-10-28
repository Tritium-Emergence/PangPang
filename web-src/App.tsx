import {useState} from 'react';
import {useTranslation} from 'react-i18next';
import {useStore} from './store';
import type {Snapshot} from './types';
import {readLocale, setLocale, type Locale} from './i18n';
import {Login} from './components/Login';
import {Sidebar} from './components/Sidebar';
import type {Goto} from './components/AttentionBar';
import {Chat} from './components/Chat';
import {Composer} from './components/Composer';
import {Inspector} from './components/Inspector';
import {WorkPane} from './components/WorkPane';
import {HostsView} from './components/HostsView';
import {SetupPanel, StatusStrip} from './components/Setup';

// The #activity pill is a SYSTEM-WIDE status read from server truth. attention_summary
// is global (not per-bot), so this is labelled as the whole workspace's state, not the
// selected bot's. Quarantine (unknown task / resource claim) is the fail-closed signal;
// otherwise the server's global working/blocked counts drive it. It NEVER re-derives
// Work status from raw turns/tasks (red line).
function ActivityPill({snap}: {snap: Snapshot}) {
  const {t} = useTranslation();
  const isolated = (snap.tasks || []).some((t) => t.status === 'unknown') || (snap.claims || []).length > 0;
  const working = Number(snap.attention_summary?.working ?? 0);
  const blocked = Number(snap.attention_summary?.blocked ?? 0);
  let text = t('header.workspaceReady');
  if (isolated) text = t('header.channelIsolated');
  else if (working > 0) text = t('header.globalWorking', {n: working});
  else if (blocked > 0) text = t('header.globalBlocked', {n: blocked});
  return <span id="activity" className="pill rounded-full bg-surface px-2 py-0.5 text-xs text-muted" title={t('header.activityTitle')}>{text}</span>;
}

// Narrow viewports have no right pane, so its 待处理 count (server attention_summary,
// verbatim) becomes one header button that opens the Work pane, where those items live.
function PendingPill({snap, open}: {snap: Snapshot; open: () => void}) {
  const {t} = useTranslation();
  const n = Number(snap.attention_summary?.needs_attention ?? 0) + Number(snap.attention_summary?.blocked ?? 0);
  if (!n) return null;
  const count = String(n);
  const pending = t('header.pending', {n});
  const word = pending.slice(0, pending.lastIndexOf(count)).trim();
  return <button id="attention-pill" className="shrink-0 rounded-full border border-warn/40 bg-warn/5 px-2 py-0.5 text-xs text-warn lg:hidden" aria-label={pending} onClick={open}><span className="attention-label">{word} </span><span className="attention-count">{count}</span></button>;
}

// Language toggle. Presentation only — flips the i18next locale (persisted to
// localStorage by setLocale) and re-renders every useTranslation() consumer; no
// server call, no reload. Shows the language it will switch TO.
function LangToggle() {
  const {i18n} = useTranslation();
  const current = (i18n.language as Locale) || readLocale();
  const next: Locale = current === 'zh-CN' ? 'en-US': 'zh-CN';
  const label = next === 'en-US' ? 'EN': '中';
  return (
    <button
      id="lang-toggle" data-locale={current} aria-label="language"
      title={next === 'en-US' ? 'Switch to English': '切换到中文'}
      className="shrink-0 rounded-lg border border-line px-2 py-1 text-xs"
      onClick={() => void setLocale(next)}
    >{label}</button>
);
}

// Composition root. Owns ONLY ephemeral UI state (selected pane); all domain truth
// comes from the single useStore() coordinator. Three-pane product shell; collapses
// responsively on narrow viewports (the center chat is always reachable).
export function App() {
  const {t} = useTranslation();
  const store = useStore();
  const [pane, setPane] = useState<'chat' | 'work' | 'hosts'>('chat');
  const [navOpen, setNavOpen] = useState(false); // mobile drawer open state

  // Work-pane / Inspector "继续此 Work" sets the one-shot target AND returns to chat so
  // the composer (which owns the target UI) is visible — mirrors old app.js view('chat').
  (window as any).__gotoChat = () => setPane('chat');

  // 待处理 navigation: a Work opens its Bot's chat with that Work current in the right
  // pane (approve / stop / recover there); anything else opens the Work pane.
  const goto: Goto = {
    work: (w) => { store.select(w.owner_bot_id); store.setContinueTarget({id: w.id, goal: w.goal || ''}); setPane('chat'); },
    pane: () => setPane('work'),
  };

  if (!store.authed) return <Login onLogin={store.login} />;
  if (!store.snapshot) return <div className="grid h-full place-items-center text-muted">{t('header.connecting')}</div>;

  const bot = store.snapshot.bots.find((b) => b.id === store.selected)
    || store.snapshot.bots.find((b) => b.lifecycle !== 'archived')
    || store.snapshot.bots[0];

  return (
    <div id="shell" className="shell-root flex overflow-hidden text-ink">
      <Sidebar store={store} pane={pane} setPane={(p) => { setPane(p); setNavOpen(false); }} navOpen={navOpen} onClose={() => setNavOpen(false)} />
      <main className="flex h-full min-h-0 min-w-0 flex-1 flex-col bg-surface">
        <header className="flex items-center gap-3 border-b border-line bg-raised px-4 py-3 sm:px-5">
          <button
            id="nav-toggle" aria-label={t('header.menu')} aria-expanded={navOpen}
            className="shrink-0 rounded-lg border border-line px-2 py-1 text-sm sm:hidden"
            onClick={() => setNavOpen((v) => !v)}
          >☰</button>
          <div className="min-w-0 flex-1">
            <h2 id="bot-name" className="truncate text-lg font-semibold">{bot?.name || t('header.defaultBotName')}</h2>
            {bot?.role ? <p id="bot-role" className="truncate text-sm text-muted">{bot.role}</p>: null}
          </div>
          <span id="connection" className="hidden text-xs text-muted sm:inline">
            {store.connection === 'connected' ? t('header.connected'): store.connection === 'reconnecting' ? t('header.reconnecting'): t('header.connectingDot')}
          </span>
          <button id="refresh" className="rounded-lg border border-line px-2 py-1 text-xs" onClick={() => void store.refresh()}>{t('header.refresh')}</button>
          <LangToggle />
          <ActivityPill snap={store.snapshot} />
          <PendingPill snap={store.snapshot} open={goto.pane} />
        </header>
        <SetupPanel store={store} />
        {pane === 'hosts' ? <StatusStrip store={store} /> : null}
        {store.notice ? (
          <div id="notice" role="status" className="flex items-center gap-2 border-b border-warn/40 bg-warn/5 px-5 py-2 text-sm text-warn">
            <span className="notice-msg">{store.notice}</span>
            <button className="notice-x ml-auto" onClick={() => store.setNotice(null)}>×</button>
          </div>
): null}
        {pane === 'chat' && bot ? (
          <>
            <Chat key={'chat:' + bot.id} store={store} botId={bot.id} />
            <Composer key={'composer:' + bot.id} store={store} botId={bot.id} />
          </>
): pane === 'work' ? (
          <WorkPane store={store} />
): (
          <HostsView store={store} />
)}
      </main>
      <Inspector store={store} botId={bot?.id} goto={goto} />
    </div>
);
}
