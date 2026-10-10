import {Modal} from './Modal';
import {useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Bot} from '../types';
import type {Store as StoreType} from '../store';
import {api} from '../api';
import mark from '../mark.png';

// Left rail. On >=sm it is a static column; on mobile it is an off-canvas drawer.
export function Sidebar({store, pane, setPane, navOpen, onClose}: {
  store: StoreType; pane: string; setPane: (p: 'chat' | 'work' | 'hosts') => void;
  navOpen: boolean; onClose: () => void;
}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const bots = snap.bots.filter((b) => b.lifecycle !== 'archived');
  const [editing, setEditing] = useState<Bot | null | undefined>(undefined);
  const unreadFor = (botId: string) => (snap.notifications || []).filter((n) => n.bot_id === botId && !n.read).length;

  const nav: {id: 'chat' | 'work' | 'hosts'; label: string}[] = [
    {id: 'chat', label: '◉ ' + t('nav.chat')}, {id: 'work', label: '▤ ' + t('nav.work')}, {id: 'hosts', label: '▦ ' + t('nav.hosts')},
  ];

  return (
    <>
      {navOpen ? <div className="fixed inset-0 z-30 bg-black/30 sm:hidden" onClick={onClose} aria-hidden="true" />: null}
      <aside className={`z-40 flex w-64 shrink-0 flex-col border-r border-line bg-raised max-sm:fixed max-sm:inset-y-0 max-sm:left-0 max-sm:transition-transform ${navOpen ? 'max-sm:translate-x-0': 'max-sm:-translate-x-full'} sm:static sm:translate-x-0`}>
        <div className="px-5 py-4">
          <div className="flex items-center gap-2">
            <img src={mark} alt="" width={32} height={32} className="h-8 w-8 shrink-0" />
            <div className="leading-tight"><div className="font-semibold">{t('nav.brand')}</div><div className="text-[10px] tracking-widest text-muted">YOUR BOT HOME</div></div>
          </div>
        </div>
        <nav className="flex flex-col gap-1 px-3">
          {nav.map((n) => <button key={n.id} data-view={n.id} onClick={() => setPane(n.id)} className={`nav rounded-lg px-3 py-2 text-left text-sm ${pane === n.id ? 'active bg-surface font-medium text-ink': 'text-muted hover:bg-surface'}`}>{n.label}</button>)}
        </nav>
        <div className="flex items-center justify-between px-5 pb-1 pt-5">
          <p className="text-xs tracking-widest text-muted">{t('nav.partners')}</p>
          <span className="flex gap-2">
            <button id="add-bot" className="text-xs text-brand" title={t('nav.createBotTitle')} onClick={() => setEditing(null)}>＋</button>
            <button id="edit-bot" className="text-xs text-muted" title={t('nav.settingsTitle')} onClick={() => setEditing(snap.bots.find((b) => b.id === store.selected) || null)}>{t('nav.settings')}</button>
          </span>
        </div>
        <div id="bot-list" className="flex-1 overflow-auto px-3">
          {bots.map((b) => {
            const unread = unreadFor(b.id);
            const busy = snap.work.some((w) => w.owner_bot_id === b.id && w.attention === 'working');
            return <button key={b.id} onClick={() => { store.select(b.id); setPane('chat'); }} className={`bot-item flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm ${b.id === store.selected ? 'selected bg-surface font-medium': 'hover:bg-surface'}`}>
              <span className="bot-avatar text-brand" aria-hidden="true">{b.kind === 'main' ? '◉': '◇'}</span>
              {busy ? <span className="dot busy text-good" title={t('nav.busyTitle')} aria-label={t('nav.busy')}>●</span>: null}
              <span className="truncate">{b.name}</span>{unread > 0 ? <span className="unread-count ml-auto rounded-full bg-brand px-1.5 text-xs text-white">{unread}</span>: null}
            </button>;
          })}
        </div>
        <div className="flex items-center justify-between border-t border-line px-5 py-3 text-xs text-muted">
          <span>{store.connection === 'connected' ? t('nav.connected'): '◌'}</span>
          <button id="logout" onClick={() => void store.logout()} className="hover:text-ink">{t('nav.logout')}</button>
        </div>
      </aside>
      {editing !== undefined ? <BotDialog key={editing?.id || 'new'} bot={editing} store={store} onClose={() => setEditing(undefined)} />: null}
    </>
);
}

function BotDialog({bot, store, onClose}: {bot: Bot | null; store: StoreType; onClose: () => void}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const [name, setName] = useState(bot?.name || '');
  const [role, setRole] = useState(bot?.role || '');
  const [instructions, setInstructions] = useState(bot?.instructions || '');
  const modelId = bot?.model_profile_id || snap.models[0]?.id || 'default';
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  return (
    <Modal id="bot-dialog" title={bot ? t('nav.editRole'): t('nav.createBot')} onClose={onClose} className="bot-modal">
      <form id="bot-form" onSubmit={async (e) => {
        e.preventDefault(); setSaving(true); setError('');
        const body: Record<string, unknown> = {name: name.trim(), role: role.trim(), instructions: instructions.trim() || role.trim(), model_profile_id: modelId};
        if (bot) body.revision = bot.revision;
        try {
          const saved = await api<Bot>(bot ? `bots/${bot.id}/update`: 'bots', body);
          store.select(saved.id); onClose(); await store.refresh();
        } catch (err: any) { setError(err?.message || t('nav.saveFailed')); }
        finally { setSaving(false); }
      }}>
        <h2 id="dialog-title" className="text-lg font-semibold">{bot ? t('nav.editRole'): t('nav.createBot')}</h2>
        <label className="mt-3 block text-sm">{t('nav.fieldName')}<input id="edit-name" required value={name} onChange={(e) => setName(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <label className="mt-2 block text-sm">{t('nav.fieldRole')}<input id="edit-role" required value={role} onChange={(e) => setRole(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <label className="mt-2 block text-sm">{t('nav.fieldInstructions')}<textarea id="edit-instructions" rows={5} value={instructions} onChange={(e) => setInstructions(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <label className="mt-2 block text-sm">{t('nav.fieldModel')}<select id="edit-model" disabled value={modelId} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5">{snap.models.map((m) => <option key={m.id} value={m.id}>{t('nav.globalModel') + ' · ' + m.model}</option>)}</select></label>
        {error ? <p role="alert" className="mt-2 text-xs text-warn">{error}</p>: null}
        <div className="mt-4 flex items-center justify-between gap-2">
          <div>{bot && bot.kind !== 'main' ? <button id="archive" type="button" className="text-xs text-warn" onClick={async () => {
            if (!confirm(t('nav.archiveConfirm'))) return;
            setSaving(true);
            try {
              await api(`bots/${bot.id}/archive`, {});
              const fallback = snap.bots.find((b) => b.id !== bot.id && b.lifecycle !== 'archived' && b.kind === 'main') || snap.bots.find((b) => b.id !== bot.id && b.lifecycle !== 'archived');
              if (fallback) store.select(fallback.id);
              onClose(); await store.refresh();
            } catch (err: any) { setError(err?.message || t('nav.archiveFailed')); }
            finally { setSaving(false); }
          }}>{t('nav.archiveBot')}</button>: null}</div>
          <div className="flex gap-2">
            <button id="close-dialog" type="button" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={onClose}>{t('nav.cancel')}</button>
            <button className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={saving}>{t('nav.save')}</button>
          </div>
        </div>
      </form>
    </Modal>
);
}
