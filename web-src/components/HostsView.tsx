import {Modal} from './Modal';
import {useEffect, useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import {api} from '../api';
import {SettingsDialog} from './Setup';
import {clock, dateTime, friendlyError} from '../display';
import {canUpgradeDevice as deviceUpgradeEligible, commitsDiffer, executionFactsFor, installationSetupTone} from '../pure';
import type {Host, HostLimits, InstallBuild} from '../types';

function shortHash(commit?: string | null) {
  if (!commit || commit === 'unknown') return '';
  return commit.length >= 7 ? commit.slice(0, 7) : commit;
}

function versionLabel(
  t: (key: string, opts?: Record<string, string>) => string,
  build?: {version?: string; commit?: string} | null,
) {
  const version = String(build?.version || '').trim();
  const commit = String(build?.commit || '').trim();
  const hash = shortHash(commit);
  if (!version && (!commit || commit === 'unknown')) return t('hosts.installVersionUnknown');
  return t('hosts.installVersion', {
    version: version || t('hosts.versionUnregistered'),
    hash: hash || t('hosts.versionHashUnknown'),
  });
}

function HostLimitsForm({host, store}: {host: Host; store: Store}) {
  const {t} = useTranslation();
  const limits = (host.limits || {}) as HostLimits;
  const cBounds = limits.concurrency_bounds || {min: 1, max: 16, default: 1};
  const tBounds = limits.timeout_bounds || {min: 30, max: 86400, default: 3600};
  const serverConcurrency = limits.max_concurrency == null ? '' : String(limits.max_concurrency);
  const serverTimeout = limits.timeout_s == null ? '' : String(limits.timeout_s);
  const [concurrency, setConcurrency] = useState(serverConcurrency);
  const [timeout, setTimeoutVal] = useState(serverTimeout);
  const [dirtyConcurrency, setDirtyConcurrency] = useState(false);
  const [dirtyTimeout, setDirtyTimeout] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!dirtyConcurrency) setConcurrency(serverConcurrency);
  }, [serverConcurrency, dirtyConcurrency]);
  useEffect(() => {
    if (!dirtyTimeout) setTimeoutVal(serverTimeout);
  }, [serverTimeout, dirtyTimeout]);

  async function save(ev: {preventDefault(): void}) {
    ev.preventDefault();
    const body: {max_concurrency?: number | null; timeout_s?: number | null} = {};
    if (dirtyConcurrency) {
      const cRaw = concurrency.trim();
      if (cRaw === '') body.max_concurrency = null;
      else {
        const n = Number(cRaw);
        if (!Number.isInteger(n) || n < cBounds.min || n > cBounds.max) {
          store.setNotice(t('hosts.invalidConcurrency'));
          return;
        }
        body.max_concurrency = n;
      }
    }
    if (dirtyTimeout) {
      const tRaw = timeout.trim();
      if (tRaw === '') body.timeout_s = null;
      else {
        const n = Number(tRaw);
        if (!Number.isInteger(n) || n < tBounds.min || n > tBounds.max) {
          store.setNotice(t('hosts.invalidTimeout'));
          return;
        }
        body.timeout_s = n;
      }
    }
    if (!('max_concurrency' in body) && !('timeout_s' in body)) {
      store.setNotice(t('hosts.noChanges'));
      return;
    }
    setSaving(true);
    try {
      const out = await api<{limits?: HostLimits}>(`hosts/${host.id}/limits`, body);
      const next = out?.limits || {};
      setConcurrency(next.max_concurrency == null ? '' : String(next.max_concurrency));
      setTimeoutVal(next.timeout_s == null ? '' : String(next.timeout_s));
      setDirtyConcurrency(false);
      setDirtyTimeout(false);
      store.setNotice(t('hosts.hostLimitsSaved'));
      await store.refresh();
    } catch (err: any) {
      store.setNotice(friendlyError(err) || t('hosts.saveFailedPrefix'));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      id={`host-limits-${host.id}`}
      className="host-limits mt-3 flex flex-wrap items-end gap-3 rounded-lg border border-line bg-surface/40 p-3"
      onSubmit={(e) => { void save(e); }}
    >
      <div className="min-w-[10rem] flex-1">
        <p className="text-xs font-medium">{t('hosts.hostLimitsTitle')}</p>
        <p className="text-[11px] text-muted">{t('hosts.hostLimitsHint')}</p>
      </div>
      <label className="flex flex-col gap-1 text-xs text-muted">
        {t('hosts.concurrency')}
        <input
          id={`host-concurrency-${host.id}`}
          className="w-28 rounded-lg border border-line bg-raised px-2 py-1 text-sm text-ink"
          inputMode="numeric"
          min={cBounds.min}
          max={cBounds.max}
          step={1}
          value={concurrency}
          placeholder={t('hosts.hostConcurrencyPlaceholder', {default: String(cBounds.default)})}
          onChange={(e) => { setConcurrency(e.target.value); setDirtyConcurrency(true); }}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-muted">
        {t('hosts.timeoutSeconds')}
        <input
          id={`host-timeout-${host.id}`}
          className="w-32 rounded-lg border border-line bg-raised px-2 py-1 text-sm text-ink"
          inputMode="numeric"
          min={tBounds.min}
          max={tBounds.max}
          step={1}
          value={timeout}
          placeholder={t('hosts.hostTimeoutPlaceholder', {default: String(tBounds.default)})}
          onChange={(e) => { setTimeoutVal(e.target.value); setDirtyTimeout(true); }}
        />
      </label>
      <button
        id={`host-limits-save-${host.id}`}
        type="submit"
        className="rounded-lg border border-line px-3 py-1.5 text-xs"
        disabled={saving}
      >{saving ? t('hosts.saving') : t('hosts.saveHostLimits')}</button>
    </form>
  );
}

// Hosts view (#hosts-view): devices + their executors, the global-model settings
// entry, and device pairing. Reads server truth (hosts/devices/installations/probes);
// never derives readiness.
export function HostsView({store}: {store: Store}) {
  const {t} = useTranslation();
  const snap = store.snapshot!;
  const cloud = (snap.cloud_build || {}) as InstallBuild;
  const [pairCode, setPairCode] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  async function upgradeCloud() {
    setBusy('cloud-plan');
    try {
      const plan = await api<{
        tip_commit: string;
        current_commit: string;
        already_current?: boolean;
      }>('setup/upgrade-plan', {});
      if (plan.already_current) {
        store.setNotice(t('hosts.upgradeCurrent'));
        return;
      }
      if (!confirm(t('hosts.upgradeConfirmCloud', {sha: plan.tip_commit}))) return;
      setBusy('cloud-upgrade');
      const result = await api<{status?: string; restarting?: boolean}>('setup/upgrade', {
        target_commit: plan.tip_commit,
      });
      if (result.restarting || result.status === 'updated') {
        store.setNotice(t('hosts.upgradeRestarting'));
      } else {
        store.setNotice(t('hosts.upgradeCurrent'));
      }
      try { await store.refresh(); } catch { /* cloud may already be restarting */ }
    } catch (e: any) {
      store.setNotice(friendlyError(e) || t('hosts.upgradePlanFailed'));
    } finally {
      setBusy(null);
    }
  }

  async function upgradeDevice(host: Host) {
    const target = String(cloud.commit || '');
    if (!/^[0-9a-f]{40}$/i.test(target)) {
      store.setNotice(t('hosts.upgradeNeedCommit'));
      return;
    }
    if (!confirm(t('hosts.upgradeConfirmDevice', {name: host.name || host.id, sha: target}))) return;
    setBusy('device:' + host.id);
    try {
      // The route now waits for the device's own command result, so a resolved
      // response is the result, not an enqueue receipt. Only the two shapes the
      // connector actually returns may claim success; anything else stays
      // "unconfirmed" rather than pretending it was queued.
      const result = await api<{status?: string; restarting?: boolean}>(`devices/${host.id}/upgrade`, {target_commit: target});
      if (result?.restarting || result?.status === 'updated') {
        store.setNotice(t('hosts.upgradeRestarting'));
      } else if (result?.status === 'already_current') {
        store.setNotice(t('hosts.upgradeCurrent'));
      } else {
        store.setNotice(t('hosts.upgradeUnconfirmed'));
      }
      await store.refresh();
    } catch (e: any) {
      // A pending wait says the result was not confirmed — not that the upgrade
      // failed; the durable command may still complete on the device.
      const code = String(e?.message || '').split(':')[0].trim();
      store.setNotice(code === 'connector_operation_pending'
        ? t('hosts.upgradePending')
        : friendlyError(e));
    } finally {
      setBusy(null);
    }
  }

  const cloudMismatchable = Boolean(cloud.commit && cloud.commit !== 'unknown');
  const canUpgradeCloud = cloud.source === 'git';

  return (
    <section id="hosts-view" className="scroll-none flex-1 overflow-auto p-5">
      <div className="panel setup-panel-top rounded-xl border border-line bg-raised p-4">
        <h3 className="font-medium">{t('hosts.connectWorkspace')}</h3>
        <p className="text-sm text-muted">{t('hosts.connectWorkspaceDesc')}</p>
        <div id="models" className="model-line mt-2 text-sm text-muted">
          {(snap.models || []).map((m) => <p key={m.id}>{m.id} · {m.model || t('hosts.noModelSet')} · {m.configured ? t('hosts.endpointConfigured'): t('hosts.endpointUnconfigured')}</p>)}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">{t('hosts.cloudServer')}</span>
          <span
            className="text-xs text-muted"
            title={cloud.commit && cloud.commit !== 'unknown' ? cloud.commit : undefined}
          >
            {versionLabel(t, cloud)}
            {cloud.ref && cloud.ref !== 'unknown' ? ` · ${cloud.ref}` : ''}
          </span>
          {canUpgradeCloud ? (
            <button
              id="upgrade-cloud"
              className="rounded-lg border border-line px-2 py-1 text-xs"
              disabled={busy != null}
              onClick={() => { void upgradeCloud(); }}
            >{t('hosts.upgradeCloud')}</button>
          ) : null}
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
          const deviceBuild = h.transport === 'local'
            ? cloud
            : {version: device?.version, commit: device?.commit};
          const mismatch = h.transport === 'connector'
            && cloudMismatchable
            && commitsDiffer(cloud.commit, device?.commit);
          const canUpgradeDevice = deviceUpgradeEligible(h, device, cloud.commit);
          // Both are "not a confirmed upgradable git checkout"; unknown must not
          // claim the install is definitely not git.
          const upgradeNotice = h.transport === 'connector' && mismatch && !canUpgradeDevice
            ? (device?.source === 'build' ? t('hosts.upgradeUnavailable')
              : device?.source === 'unknown' ? t('hosts.upgradeSourceUnknown') : null)
            : null;
          return (
            <section key={h.id} className="device-section rounded-xl border border-line bg-raised p-4">
              <div className="device-head flex items-start justify-between gap-3">
                <div className="device-meta min-w-0">
                  <h3 className="device-name font-medium">{h.name || h.id}</h3>
                  <span className="device-os text-xs text-muted">{h.os || t('hosts.osUnregistered')}</span>
                  <p
                    className={`mt-1 text-xs ${mismatch ? 'text-warn' : 'text-muted'}`}
                    title={deviceBuild.commit && deviceBuild.commit !== 'unknown' ? deviceBuild.commit : undefined}
                  >
                    {versionLabel(t, deviceBuild)}
                    {mismatch ? ` · ${t('hosts.versionMismatch')}` : ''}
                  </p>
                  {upgradeNotice ? (
                    <p className="mt-1 text-xs text-muted" data-testid="device-upgrade-unavailable">{upgradeNotice}</p>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
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
                  {canUpgradeDevice ? (
                    <button
                      className="rounded-lg border border-line px-2 py-1 text-xs"
                      disabled={busy != null}
                      onClick={() => { void upgradeDevice(h); }}
                    >{t('hosts.upgradeDevice')}</button>
                  ) : null}
                  {h.transport === 'connector' && !device?.revoked ? (
                    <button className="rounded-lg border border-line px-2 py-1 text-xs" onClick={async () => {
                      if (!confirm(t('hosts.revokeConfirm'))) return;
                      try { await api(`devices/${h.id}/revoke`, {}); await store.refresh(); }
                      catch (e: any) { store.setNotice(e?.message || t('hosts.revokeFailed')); }
                    }}>{t('hosts.revokeDevice')}</button>
): null}
                </div>
              </div>
              <HostLimitsForm host={h} store={store} />
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
