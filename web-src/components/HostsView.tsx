import {Modal} from './Modal';
import {useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import {api} from '../api';
import {SettingsDialog} from './Setup';
import {clock, dateTime, friendlyError} from '../display';
import {executionFactsFor, installationSetupTone} from '../pure';

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
                  {h.transport === 'connector' ? (
                    <label className="flex items-center gap-1 text-xs text-muted" title={t('hosts.executionLevelHint')}>
                      {t('hosts.executionLevel')}
                      <select className="rounded-lg border border-line bg-raised px-1.5 py-1 text-xs" defaultValue={h.execution_level || 'L0'} onChange={async (e) => {
                        try { await api(`hosts/${h.id}/execution-level`, {level: e.target.value}); await store.refresh(); }
                        catch (err: any) { store.setNotice(friendlyError(err)); }
                      }}>{['L0', 'L1', 'L2'].map((lv) => <option key={lv} value={lv}>{lv}</option>)}</select>
                    </label>
): null}
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
                  const facts = executionFactsFor(snap.setup.capabilities, e.id as string);
                  const tone = installationSetupTone(facts, probe);
                  const version = String(probe?.version || t('hosts.versionUnregistered'));
                  const statusText = tone === 'ready'
                    ? t('hosts.readyVerified', {version})
                    : tone === 'discovered_not_verified'
                      ? t('hosts.discoveredNotVerified', {version})
                      : probe
                        ? t('hosts.unavailable', {reason: String(probe.reason || '').slice(0, 60)})
                        : t('hosts.notProbed');
                  const statusClass = tone === 'ready' ? 'text-good' : probe && tone !== 'discovered_not_verified' ? 'text-warn' : 'text-muted';
                  return (
                    <div key={e.id as string} className="panel rounded-lg border border-line p-3">
                      <h4 className="text-sm font-medium">{e.id as string}</h4>
                      <p className={`text-xs ${statusClass}`} data-execution-verified={tone === 'ready' ? 'true' : tone === 'discovered_not_verified' ? 'false' : undefined}>
                        {statusText}
                      </p>
                      <button className="mt-1 text-xs text-brand" onClick={async () => { try { await api(`installations/${e.id}/probe`, {}); await store.refresh(); } catch (err: any) { store.setNotice(err?.message); } }}>{t('hosts.probeInstall')}</button>
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
