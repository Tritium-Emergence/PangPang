import {Modal} from './Modal';
import {useEffect, useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import type {Installation} from '../types';
import {api} from '../api';
import {SettingsDialog} from './Setup';
import {clock, dateTime, friendlyError} from '../display';

// Hosts view (#hosts-view): devices + their executors, the global-model settings
// entry, and device pairing. Reads server truth (hosts/devices/installations/probes);
// never derives readiness.
export function HostsView({store}: {store: Store}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const [pairCode, setPairCode] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);

  return (
    <section id="hosts-view" className="scroll-none flex-1 overflow-auto p-5">
      <div className="panel setup-panel-top rounded-xl border border-line bg-raised p-4">
        <h3 className="font-medium">{t('hosts.connectWorkspace')}</h3>
        <p className="text-sm text-muted">{t('hosts.connectWorkspaceDesc')}</p>
        <div id="models" className="model-line mt-2 text-sm text-muted">
          {(snap.models || []).map((m) => <p key={m.id}>{m.id} · {m.model || t('hosts.noModelSet')} · {m.configured ? t('hosts.endpointConfigured'): t('hosts.endpointUnconfigured')}</p>)}
        </div>
        <div className="setup-actions mt-2 flex gap-2">
          <button id="settings" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={() => setShowSettings(true)}>{t('hosts.setGlobalModel')}</button>
          <button id="pair-device" className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white" onClick={async () => {
            try { const r = await api<any>('setup/pairing', {}); setPairCode(r.code); } catch (e: any) { store.setNotice(e?.message); }
          }}>{t('hosts.connectNewComputer')}</button>
        </div>
      </div>

      <h2 className="mt-6 text-sm font-semibold tracking-widest text-muted">{t('hosts.myComputers')}</h2>
      <div id="hosts" className="mt-2 space-y-3">
        {(snap.hosts || []).map((h) => {
          const device = (snap.devices || []).find((d) => d.id === h.id);
          const insts = (snap.installations || []).filter((e) => e.host_id === h.id);
          const statusText = h.transport === 'connector'
            ? (device?.revoked ? t('hosts.deviceRevoked'): (device?.online ? t('hosts.online'): t('hosts.offline')))
: t('hosts.localDevice');
          const inventory = h.transport === 'connector'
            ? (((snap.setup.inventory || []) as any[]).find((x) => x.id === h.id)?.items || []) as any[]
: [];
          const registered = new Set(insts.map((e) => String(e.adapter || '')));
          const unregistered = inventory.filter((x) => x.adapter && !registered.has(String(x.adapter)));
          const pendingAdapters = new Set((snap.setup.requests || [])
            .filter((r) => r.host_id === h.id && ['needs_approval', 'configuring'].includes(r.status))
            .map((r) => String((r.proposal || {}).adapter || '')));
          return (
            <section key={h.id} className="device-section rounded-xl border border-line bg-raised p-4">
              <div className="device-head flex items-start justify-between gap-3">
                <div className="device-meta">
                  <h3 className="device-name font-medium">{h.name || h.id}</h3>
                  <span className="device-os text-xs text-muted">{h.os || t('hosts.osUnregistered')}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`device-status text-xs ${device?.online || h.transport === 'local' ? 'text-good': 'text-muted'}`}>{statusText}{device?.last_seen ? <span title={dateTime(device.last_seen)}> · {t('hosts.asOf', {time: clock(device.last_seen)})}</span>: null}</span>
                  {h.transport === 'connector' && !device?.revoked ? (
                    <button className="rounded-lg border border-line px-2 py-1 text-xs" onClick={async () => {
                      if (!confirm(t('hosts.revokeConfirm'))) return;
                      try { await api(`devices/${h.id}/revoke`, {}); await store.refresh(); }
                      catch (e: any) { store.setNotice(e?.message || t('hosts.revokeFailed')); }
                    }}>{t('hosts.revokeDevice')}</button>
): null}
                </div>
              </div>
              <div className="exec-grid mt-2 grid grid-cols-1 gap-2 lg:grid-cols-2">
                {insts.length ? insts.map((e) => {
                  const probe = (snap.probes || []).find((p) => p.id === e.id);
                  return (
                    <div key={e.id as string} className="panel rounded-lg border border-line p-3">
                      <h4 className="text-sm font-medium">{e.id as string}</h4>
                      <p className={`text-xs ${probe?.available ? 'text-good': probe ? 'text-warn': 'text-muted'}`}>
                        {probe ? (probe.available ? t('hosts.probed', {version: String(probe.version || t('hosts.versionUnregistered'))}): t('hosts.unavailable', {reason: String(probe.reason || '').slice(0, 60)})): t('hosts.notProbed')}
                      </p>
                      <button className="mt-1 text-xs text-brand" onClick={async () => { try { await api(`installations/${e.id}/probe`, {}); await store.refresh(); } catch (err: any) { store.setNotice(err?.message); } }}>{t('hosts.probeInstall')}</button>
                      <InstallationConfig installation={e} store={store} purposeOnly={h.transport !== 'local'} />
                    </div>
);
                }): <p className="text-sm text-muted">{t('hosts.noExecutors')}</p>}
              </div>
              {unregistered.length ? (
                <div className="mt-2">
                  <p className="text-xs font-medium text-muted">{t('hosts.unregisteredExecutors')}</p>
                  <div className="mt-1 grid grid-cols-1 gap-2 lg:grid-cols-2">
                    {unregistered.map((x) => (
                      <div key={String(x.adapter)} className="panel rounded-lg border border-dashed border-line p-3">
                        <h4 className="text-sm font-medium">{String(x.adapter)}</h4>
                        <p className="text-xs text-muted">{String(x.version || t('hosts.versionUnregistered'))}</p>
                        {pendingAdapters.has(String(x.adapter)) ? (
                          <p className="mt-1 text-xs text-warn">{t('hosts.registerPending')}</p>
): (
                          <button className="mt-1 text-xs text-brand" onClick={async () => {
                            try { await api('setup/request', {host_id: h.id, action: 'configure', adapter: String(x.adapter)}); await store.refresh(); }
                            catch (err: any) { store.setNotice(err?.message); }
                          }}>{t('hosts.registerExecutor')}</button>
)}
                      </div>
))}
                  </div>
</div>
): null}
            </section>
);
        })}
      </div>

      {pairCode != null ? (
        <Modal id="pair-dialog" title={t('hosts.pairDialogTitle')} onClose={() => setPairCode(null)} className="pair-modal">
          <h2 className="text-lg font-semibold">{t('hosts.pairDialogTitle')}</h2>
          <p className="mt-1 text-sm text-muted">{t('hosts.pairDialogDesc')}</p>
          <pre id="pair-code" className="mt-2 rounded-lg bg-surface p-3 text-sm">{pairCode}</pre>
          <p className="text-xs text-muted">{t('hosts.pairCodeHint')}</p>
          <div className="mt-3 flex justify-end">
            <button id="pair-close" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={() => setPairCode(null)}>{t('hosts.close')}</button>
          </div>
        </Modal>
): null}
      {showSettings ? <SettingsDialog store={store} onClose={() => setShowSettings(false)} />: null}
    </section>
);
}

type InstallValues = {
  max_concurrency: number; timeout_s: number; cwd: string; enabled: boolean;
  priority: number | null; use_when: string;
};
function installValues(e: Installation): InstallValues {
  const x = e as any;
  return {
    max_concurrency: Number(x.max_concurrency ?? 1),
    timeout_s: Number(x.timeout_s ?? 3600),
    cwd: String(x.cwd || ''),
    enabled: x.enabled !== false,
    priority: x.priority == null ? null: Number(x.priority),
    use_when: String(x.use_when || ''),
  };
}

function InstallationConfig({installation, store, purposeOnly = false}: {installation: Installation; store: Store; purposeOnly?: boolean}) {
  const {t} = useTranslation();
  const signature = JSON.stringify(installValues(installation));
  const [baseline, setBaseline] = useState<InstallValues>(() => installValues(installation));
  const [mc, setMc] = useState(String(baseline.max_concurrency));
  const [timeout, setTimeout] = useState(String(baseline.timeout_s));
  const [priority, setPriority] = useState(baseline.priority == null ? '': String(baseline.priority));
  const [cwd, setCwd] = useState(baseline.cwd);
  const [useWhen, setUseWhen] = useState(baseline.use_when);
  const [enabled, setEnabled] = useState(baseline.enabled);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState('');

  useEffect(() => {
    if (dirty || saving) return;
    const v = installValues(installation);
    setBaseline(v); setMc(String(v.max_concurrency)); setTimeout(String(v.timeout_s));
    setPriority(v.priority == null ? '': String(v.priority)); setCwd(v.cwd);
    setUseWhen(v.use_when); setEnabled(v.enabled);
  // signature intentionally tracks server snapshot changes.
  }, [signature, dirty, saving, installation]);

  const mark = () => { setDirty(true); setStatus(t('hosts.unsavedChanges')); };
  const save = async () => {
    const pr = priority.trim() === '' ? null: Number(priority);
    if (pr != null && (!Number.isInteger(pr) || pr < 1 || pr > 999)) { setStatus(t('hosts.invalidPriority')); return; }
    if (useWhen.length > 500) { setStatus(t('hosts.invalidUseWhen')); return; }
    const next: InstallValues | null = purposeOnly
      ? {max_concurrency: baseline.max_concurrency, timeout_s: baseline.timeout_s, cwd: baseline.cwd, enabled: baseline.enabled, priority: pr, use_when: useWhen.trim()}
      : (() => {
        const max = Number(mc), sec = Number(timeout);
        if (!Number.isInteger(max) || max < 1 || max > 16) { setStatus(t('hosts.invalidConcurrency')); return null; }
        if (!Number.isFinite(sec) || sec < 30 || sec > 86400) { setStatus(t('hosts.invalidTimeout')); return null; }
        return {max_concurrency: max, timeout_s: sec, cwd: cwd.trim(), enabled, priority: pr, use_when: useWhen.trim()};
      })();
    if (!next) return;
    const changes: Record<string, unknown> = {};
    const keys = (purposeOnly ? ['priority', 'use_when']: Object.keys(next)) as (keyof InstallValues)[];
    for (const k of keys) if (next[k] !== baseline[k]) changes[k] = next[k];
    if (!Object.keys(changes).length) { setDirty(false); setStatus(t('hosts.noChanges')); return; }
    setSaving(true); setStatus(t('hosts.saving'));
    try {
      const saved = await api<Installation>(`installations/${installation.id}/config`, changes);
      const v = installValues(saved); setBaseline(v); setDirty(false); setStatus(t('hosts.configSaved'));
      setMc(String(v.max_concurrency)); setTimeout(String(v.timeout_s)); setPriority(v.priority == null ? '': String(v.priority));
      setCwd(v.cwd); setUseWhen(v.use_when); setEnabled(v.enabled);
      await store.refresh();
    } catch (e: any) { setStatus(t('hosts.saveFailedPrefix') + friendlyError(e)); }
    finally { setSaving(false); }
  };

  return (
    <details className="mt-2 border-t border-line pt-2" data-config={purposeOnly ? 'purpose': 'full'}>
      <summary className="cursor-pointer text-xs text-brand">{t('hosts.configureExecutor')}</summary>
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
        {purposeOnly ? null: <>
          <label>{t('hosts.concurrency')}<input type="number" min={1} max={16} value={mc} onChange={(e) => { setMc(e.target.value); mark(); }} className="mt-1 w-full rounded border border-line px-2 py-1" /></label>
          <label>{t('hosts.timeoutSeconds')}<input type="number" min={30} max={86400} value={timeout} onChange={(e) => { setTimeout(e.target.value); mark(); }} className="mt-1 w-full rounded border border-line px-2 py-1" /></label>
        </>}
        <label>{t('hosts.priority')}<input type="number" min={1} max={999} placeholder={t('hosts.priorityPlaceholder')} value={priority} onChange={(e) => { setPriority(e.target.value); mark(); }} className="mt-1 w-full rounded border border-line px-2 py-1" /></label>
        {purposeOnly ? null: <label className="flex items-end gap-2 pb-1"><input type="checkbox" checked={enabled} onChange={(e) => { setEnabled(e.target.checked); mark(); }} />{t('hosts.enabled')}</label>}
        {purposeOnly ? null: <label className="col-span-2">{t('hosts.workingDir')}<input value={cwd} onChange={(e) => { setCwd(e.target.value); mark(); }} className="mt-1 w-full rounded border border-line px-2 py-1" /></label>}
        <label className="col-span-2">{t('hosts.useWhen')}<input value={useWhen} maxLength={500} onChange={(e) => { setUseWhen(e.target.value); mark(); }} className="mt-1 w-full rounded border border-line px-2 py-1" /></label>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <button type="button" className="rounded bg-brand px-2 py-1 text-xs text-white disabled:opacity-50" disabled={saving} onClick={() => void save()}>{t('hosts.saveConfig')}</button>
        <span role="status" className="text-xs text-muted">{status}</span>
      </div>
    </details>
);
}
