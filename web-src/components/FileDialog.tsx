import {useEffect, useRef, useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import {api} from '../api';
import {fileSize, friendlyError} from '../display';
import {Modal} from './Modal';

interface Entry {name: string; type: 'dir' | 'file' | 'link'; size?: number | null}
interface Browse {path: string; parent?: string | null; entries: Entry[]; truncated?: boolean}

// Device-file reference dialog (#file-dialog). Browses a host's allowed workspace via
// POST hosts/{id}/browse, lets the user pick a file, then references it via
// POST bots/{id}/files -> the artifact becomes a composer attachment. The file is read
// and version-checked on the owning device; the original is never modified.
export function FileDialog({store, botId, onClose}: {store: Store; botId: string; onClose: () => void}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const hosts = [...(snap.hosts || [])].sort((a, b) => (a.transport === 'local' ? 0: 1) - (b.transport === 'local' ? 0: 1));
  const [host, setHost] = useState(hosts[0]?.id || '');
  const [browse, setBrowse] = useState<Browse | null>(null);
  const [picked, setPicked] = useState('');
  const [err, setErr] = useState('');

  const sequence = useRef(0);
  const [saving, setSaving] = useState(false);
  useEffect(() => () => { sequence.current++; }, []);
  const load = async (path: string) => {
    const seq = ++sequence.current;
    setErr(''); setPicked(''); setBrowse(null);
    try { const result = await api<Browse>(`hosts/${encodeURIComponent(host)}/browse`, {path}); if (seq === sequence.current) setBrowse(result); }
    catch (e) { if (seq === sequence.current) { setErr(friendlyError(e)); setBrowse({path: '', entries: []}); } }
  };
  useEffect(() => { if (host) void load(''); /* eslint-disable-next-line */ }, [host]);

  const sep = (browse?.path || '').includes('\\') && !(browse?.path || '').includes('/') ? '\\': '/';
  const full = (n: string) => (browse?.path ? browse.path.replace(/[\\/]$/, '') + sep + n: n);

  const save = async () => {
    if (saving || !picked) return;
    setSaving(true);
    const token = store.selectionToken(); // capture the selection epoch at reference start
    try {
      const a = await api<any>(`bots/${botId}/files`, {host_id: host, path: picked});
      // Late completion may only touch current UI state if the selection epoch is unchanged
      // (handles both switch-away and A→B→A).
      if (store.liveSelected() !== botId || !store.selectionCurrent(token)) { onClose(); return; }
      store.setAttached((prev) => [...prev, {id: a.id, name: a.name || a.path.split(/[\\/]/).pop()}]);
      onClose();
    } catch (e) { if (store.liveSelected() === botId && store.selectionCurrent(token)) setErr(friendlyError(e)); }
    finally { setSaving(false); }
  };

  return (
    <Modal id="file-dialog" title={t('file.title')} onClose={onClose}>
      <h2 className="text-lg font-semibold">{t('file.title')}</h2>
      <label className="mt-3 block text-sm">{t('file.deviceLabel')}
        <select id="file-host" value={host} onChange={(e) => setHost(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5">
          {hosts.map((h) => <option key={h.id} value={h.id}>{(h.transport === 'local' ? t('file.mainServer') + ' · ': t('file.onlineDevice') + ' · ') + (h.name || h.id)}</option>)}
        </select>
      </label>
      <div id="file-browser" className="file-browser mt-3 overflow-auto rounded-lg border border-line" aria-live="polite">
        {browse ? (
          <>
            <div className="browse-head flex items-center justify-between border-b border-line px-3 py-1.5 text-xs text-muted">
              <span className="browse-path truncate">{browse.path || t('file.workingDir')}</span>
              {browse.path ? <button type="button" className="ghost" onClick={() => void load(browse.parent || '')}>{t('file.up')}</button>: null}
            </div>
            <div className="browse-list">
              {browse.entries.map((e) => (
                <button
                  key={e.name}
                  type="button"
                  className={`browse-item ${e.type} flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-surface ${picked === full(e.name) ? 'picked bg-surface': ''}`}
                  onClick={() => { if (e.type === 'dir') void load(full(e.name)); else setPicked(full(e.name)); }}
                >
                  <span className="browse-ico">{e.type === 'dir' ? '▸': e.type === 'link' ? '↪': '·'}</span>
                  <span className="browse-name">{e.name}</span>
                  {e.type === 'file' && e.size != null ? <span className="browse-size">{fileSize(e.size)}</span>: null}
                </button>
))}
              {browse.truncated ? <p className="px-3 py-2 text-xs text-muted">{t('file.truncated')}</p>: null}
              {browse.entries.length === 0 ? <p className="px-3 py-2 text-sm text-muted">{t('file.emptyDir')}</p>: null}
            </div>
          </>
): <p className="px-3 py-2 text-sm text-muted">{t('file.loading')}</p>}
      </div>
      <p className="mt-2 text-xs text-muted">{t('file.hint')}</p>
      <p id="file-selected" className="mt-2 text-xs text-muted">{picked ? t('file.selectedPrefix') + picked: t('file.pickPrompt')}</p>
      {err ? <p className="mt-1 text-xs text-warn">{err}</p>: null}
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" id="file-close" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={onClose}>{t('file.cancel')}</button>
        <button type="button" id="file-save" className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={!picked || saving} onClick={() => void save()}>{t('file.reference')}</button>
      </div>
    </Modal>
);
}
