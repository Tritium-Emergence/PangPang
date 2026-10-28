import {Modal} from './Modal';
import {useEffect, useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import type {Capability, SetupRequest} from '../types';
import {api} from '../api';
import {sealCredential, type SecretTarget} from '../crypto';
import {capabilityLabel, capabilityStateLabel, friendlyError, DEFAULT_OUTPUT_TOKENS} from '../display';
import {executorSetupTone} from '../pure';

// Conversational-setup surface. Mirrors setup.js: the model settings dialog, the
// pending setup-request panel (approve + sealed-secret), and credential sealing. RCB:
// the conversation PROPOSES setup; the control plane COMMITS it. The browser only
// seals a secret for the target device — it never writes config or holds authority.

export function SettingsDialog({store, onClose}: {store: Store; onClose: () => void}) {
  const {t} = useTranslation();
  const model = store.snapshot!.setup.model;
  const [endpoint, setEndpoint] = useState(model.endpoint);
  const [provider, setProvider] = useState(model.provider || 'custom');
  const [protocol, setProtocol] = useState(model.api || 'openai-completions');
  const [reasoning, setReasoning] = useState(model.reasoning_level || 'default');
  const [format, setFormat] = useState(model.thinking_format || 'auto');
  const [budgetField, setBudgetField] = useState(model.thinking_budget_field || '');
  const [budget, setBudget] = useState(model.thinking_budget?.toString() || '');
  const [keepLegacy, setKeepLegacy] = useState(true);
  const [tokenField, setTokenField] = useState(model.max_tokens_field || 'auto');
  const [outputTokens, setOutputTokens] = useState(String(model.output_tokens || DEFAULT_OUTPUT_TOKENS));
  const [contextWindow, setContextWindow] = useState(model.context_window_tokens ? String(model.context_window_tokens) : '');
  const [concurrency, setConcurrency] = useState(String(store.snapshot!.setup.model_max_concurrency || 4));
  type CatalogModel = {api: string; baseUrl: string; name: string; reasoning: boolean; maxTokens: number; contextWindow: number; output_floor: number; output_ceiling: number; thinkingLevelMap?: Record<string, string | null>};
  type CustomBounds = {output_floor: number; output_ceiling: number; fitted_output: number};
  const [catalog, setCatalog] = useState<Record<string, Record<string, CatalogModel>>>({});
  const [customBounds, setCustomBounds] = useState<CustomBounds | null>(null);
  useEffect(() => {
    let active = true;
    api<{providers: typeof catalog; custom: CustomBounds}>('setup/model-catalog').then(v => {
      if (!active) return;
      setCatalog(v.providers);
      setCustomBounds(v.custom);
    }).catch(() => {if (active) setError(t('setup.modelCatalogLoadFailed'));});
    return () => {active = false;};
  }, []);
  useEffect(() => {
    if (provider !== 'custom' || !contextWindow) return;
    const typed = Number(contextWindow);
    if (!Number.isInteger(typed) || typed === model.context_window_tokens) return;
    let active = true;
    const timer = window.setTimeout(() => {
      api<{custom: CustomBounds}>(`setup/model-catalog?context_window_tokens=${typed}`).then(v => {
        if (!active) return;
        setCustomBounds(v.custom);
        setOutputTokens(prev => {
          const current = Number(prev);
          if (!Number.isInteger(current) || current < v.custom.output_floor || current > v.custom.output_ceiling) return String(v.custom.fitted_output);
          return prev;
        });
      }).catch(() => {});
    }, 200);
    return () => {active = false; window.clearTimeout(timer);};
  }, [provider, contextWindow, model.context_window_tokens]);
  const [name, setName] = useState(model.model);
  const [secret, setSecret] = useState('');
  const [noAuth, setNoAuth] = useState(false);
  const [vision, setVision] = useState(model.vision === true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState('');

  const known = catalog[provider]?.[name];
  const applyCap = (cap?: number) => { if (typeof cap === 'number') setOutputTokens(String(cap)); };
  const resetThinking = (selected?: {output_floor: number}) => {
    setReasoning('default'); setFormat('auto'); setBudgetField(''); setBudget(''); setTokenField('auto');
    applyCap(selected ? selected.output_floor : customBounds?.fitted_output);
  };
  const outputMin = known ? known.output_floor : customBounds?.output_floor;
  const outputMax = known ? known.output_ceiling : customBounds?.output_ceiling;
  const levels = ['default', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].filter(level =>
    level === 'default' || (!known || (known.reasoning || level === 'off') && known.thinkingLevelMap?.[level] !== null));
  return (
    <Modal id="settings-dialog" title={t('setup.modelDialogTitle')} onClose={() => { setSecret(''); onClose(); }} className="settings-modal">
      <form id="settings-form" onSubmit={async (e) => {
        e.preventDefault(); setSaving(true); setError(t('setup.validatingToolCalls'));
        const s = secret; setSecret('');
        try { const r = await api<any>('setup/model', {endpoint, model: name, secret: s, no_auth: noAuth, vision, provider, api: protocol, reasoning_level: reasoning, thinking_format: format, thinking_budget_field: budgetField, thinking_budget: budget ? Number(budget): null, max_tokens_field: tokenField, output_tokens: outputTokens ? Number(outputTokens): null, context_window_tokens: provider === 'custom' && contextWindow ? Number(contextWindow): null, model_max_concurrency: concurrency ? Number(concurrency): null, keep_generation_settings: keepLegacy}); onClose(); await store.refresh(); if (r?.vision_unverified) store.setNotice(t('setup.visionDisabled')); }
        catch (err: any) { setError(friendlyError(err) || t('setup.saveFailed')); } finally { setSaving(false); }
      }}>
        <h2 className="text-lg font-semibold">{t('setup.modelDialogTitle')}</h2>
        <p className="mt-1 text-sm text-muted">{t('setup.modelDialogDesc')}</p>
        <label className="mt-3 block text-sm">{t('setup.provider')}<select id="model-provider" className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" value={provider} onChange={e => {
          const value = e.target.value; setProvider(value); setName(''); setSecret(''); setNoAuth(false); resetThinking();
          const first = Object.values(catalog[value] || {})[0]; setEndpoint(first?.baseUrl || ''); setProtocol(first?.api || 'openai-completions');
        }}><option value="custom">{t('setup.providerCustom')}</option>
          {Object.keys(catalog).sort().map(p => <option key={p} value={p}>{p}</option>)}
          {provider !== 'custom' && !catalog[provider] ? <option value={provider}>{provider}</option>: null}
        </select></label>
        <label className="mt-2 block text-sm">{t('setup.modelName')}<input id="model-name" list="pi-models" required value={name} onChange={e => {
          const selected = catalog[provider]?.[e.target.value]; setName(e.target.value); resetThinking(selected);
          if (selected) {setProtocol(selected.api); setEndpoint(selected.baseUrl);}
        }} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <datalist id="pi-models">{Object.entries(catalog[provider] || {}).map(([id, m]) => <option key={id} value={id}>{m.name}</option>)}</datalist>
        {provider === 'custom' ? <label className="mt-2 block text-sm">{t('setup.apiProtocol')}<select id="model-api" value={protocol} onChange={e => {setProtocol(e.target.value); resetThinking();}} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5">
          <option value="openai-completions">{t('setup.protocolOpenaiCompletions')}</option>
          <option value="openai-responses">OpenAI Responses</option><option value="anthropic-messages">Anthropic Messages</option>
          <option value="google-generative-ai">Google Gemini</option>
        </select></label>: null}
        <label className="mt-2 block text-sm">{t('setup.apiBaseUrl')}<input id="model-endpoint" type="url" required value={endpoint} onChange={(e) => setEndpoint(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        {provider === 'custom' ? <label className="mt-2 block text-sm">{t('setup.contextWindow')}<input id="model-context-window" type="number" min={5120} max={2000000} step="1" value={contextWindow} onChange={e => setContextWindow(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>: null}
        <label className="mt-2 block text-sm">{t('setup.outputCap')}<input id="model-output-tokens" type="number" required min={outputMin} max={outputMax} step="1" value={outputTokens} onChange={e => setOutputTokens(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <p className="mt-1 text-xs text-muted">{t('setup.outputCapHint', {min: outputMin})}</p>
        <label className="mt-2 block text-sm">{t('setup.concurrency')}<input id="model-concurrency" type="number" required min={1} max={16} step="1" value={concurrency} onChange={e => setConcurrency(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        {store.snapshot?.setup.model_concurrency_restart_required ? <p className="mt-1 text-xs text-warn">{t('setup.concurrencyRestart')}</p>: null}
        <label className="mt-2 block text-sm">{t('setup.reasoningLevel')}<select id="model-reasoning" value={reasoning} onChange={e => setReasoning(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5">
          {levels.map(level => <option key={level} value={level}>{level === 'default' ? t('setup.reasoningDefault'): level === 'off' ? t('setup.reasoningOff'): level}</option>)}
        </select></label>
        {protocol === 'openai-completions' ? <details className="mt-2 text-sm"><summary>{t('setup.advancedSettings')}</summary>
          <p className="mt-1 text-xs text-muted">{t('setup.advancedHint')}</p>
          <label className="mt-2 block">{t('setup.thinkingFormat')}<select id="model-thinking-format" value={format} onChange={e => setFormat(e.target.value)} className="ml-2 border border-line">
            {['auto', 'openai', 'qwen', 'qwen-chat-template', 'deepseek', 'zai', 'openrouter', 'together'].map(f => <option key={f} value={f}>{f === 'auto' ? t('setup.thinkingFormatAuto'): f}</option>)}
          </select></label>
          <label className="mt-2 block">{t('setup.budgetField')}<select id="model-budget-field" value={budgetField} onChange={e => setBudgetField(e.target.value)} className="ml-2 border border-line">
            <option value="">{t('setup.budgetFieldNone')}</option>{['thinking_token_budget', 'thinking_budget', 'thinking_budget_tokens'].map(f => <option key={f}>{f}</option>)}
          </select></label>
          <label className="mt-2 block">{t('setup.thinkingBudget')}<input id="model-thinking-budget" type="number" min="1" max="1048576" value={budget} onChange={e => setBudget(e.target.value)} className="ml-2 w-28 border border-line" /></label>
          <label className="mt-2 block">{t('setup.tokenField')}<select id="model-token-field" value={tokenField} onChange={e => setTokenField(e.target.value)} className="ml-2 border border-line">
            {['auto', 'max_tokens', 'max_completion_tokens'].map(f => <option key={f}>{f}</option>)}
          </select></label>
        </details>: null}
        {model.has_legacy_generation_settings ? <label className="mt-2 flex items-center gap-2 text-xs text-muted"><input id="model-keep-legacy" type="checkbox" checked={keepLegacy} onChange={e => setKeepLegacy(e.target.checked)} />{t('setup.keepLegacy')}</label>: null}
        <p className="mt-1 text-xs text-muted">{t('setup.endpointHint')}</p>
        <label className="mt-2 block text-sm">{t('setup.apiKey')}<input id="model-secret" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <label className="mt-2 flex items-center gap-2 text-sm"><input id="model-no-auth" type="checkbox" checked={noAuth} onChange={(e) => setNoAuth(e.target.checked)} />{t('setup.noAuth')}</label>
        <label className="mt-1 flex items-center gap-2 text-sm"><input id="model-vision" type="checkbox" checked={vision} onChange={(e) => setVision(e.target.checked)} />{t('setup.vision')}</label>
        <p className="mt-2 text-xs text-muted">{t('setup.keyStorageHint')}</p>
        {error ? <p id="model-error" role="alert" className="mt-1 text-xs text-warn">{error}</p>: null}
        {status ? <p role="status" className="mt-1 text-xs text-good">{status}</p>: null}
        <div className="mt-3 flex items-center justify-between gap-2">
          <div>
            {model.has_secret ? <button id="model-revoke" type="button" className="text-xs text-warn" disabled={saving} onClick={async () => {
              if (!confirm(t('setup.revokeConfirm'))) return;
              setSaving(true); setError(''); setStatus('');
              try { await api('setup/model-key/revoke', {}); setSecret(''); setStatus(t('setup.keyRevoked')); await store.refresh(); }
              catch (err: any) { setError(err?.message || t('setup.revokeFailed')); }
              finally { setSaving(false); }
            }}>{t('setup.revokeKey')}</button>: null}
          </div>
          <div className="flex gap-2">
          <button type="button" id="settings-close" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={() => { setSecret(''); onClose(); }}>{t('setup.cancel')}</button>
          <button id="model-save" className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={saving}>{t('setup.validateAndSave')}</button>
          </div>
        </div>
      </form>
    </Modal>
);
}

function CapabilityList({rows, envRows, onSealEnv, onClearEnv}: {
  rows: Capability[];
  envRows: {name: string; configured: boolean; sealed: boolean}[];
  onSealEnv: (name: string) => void;
  onClearEnv: (name: string) => void;
}) {
  const {t, i18n} = useTranslation();
  if (!rows.length && !envRows.length) return null;
  // Never render row.operator_action (backend's Chinese prose). Map the stable
  // operator_action_code to a localized string; set_coordinator_env interpolates the
  // env var from detail. Unknown codes fall back to a generic hint + the raw code.
  const actionText = (row: Capability): string | null => {
    const code = row.operator_action_code;
    if (!code) return null;
    const key = 'capabilityAction.' + code;
    if (i18n.exists(key)) return t(key, {var: (row.detail?.env_var as string) || ''});
    return `${t('capabilityAction.generic')} (${code})`;
  };
  return (
    <section id="capability-list" className="border-b border-line bg-raised px-5 py-2.5">
      <details open>
      <summary className="cursor-pointer text-xs text-muted">{t('setup.statusBar')}</summary>
      <ul className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1.5">
        {rows.map((row) => {
          const action = actionText(row);
          const label = capabilityLabel[row.capability] ? t(capabilityLabel[row.capability]): row.capability;
          // Admission state stays on data-state. The ready word is execution_verified
          // on the selected executor; discovery alone is not that receipt.
          const tone = executorSetupTone(row);
          const stateKey = tone === 'discovered_not_verified'
            ? 'capabilityState.discoveredNotVerified'
            : capabilityStateLabel[row.state];
          const state = stateKey ? t(stateKey): row.state;
          // Dot color maps the raw state code (judgment is server-side; this is
          // presentation). ready -> good, unless execution is not verified.
          // blocked/unconfigured -> warn; else muted.
          // A problem state (has an action) shows the action inline so touch/keyboard
          // users can discover the fix; a healthy pill stays compact (title only).
          // Jev/semantic check is optional. unconfigured must not look like a fault.
          // enforce without a key stays blocked and keeps the operator action.
          const optionalGate = row.capability === 'semantic_gate' && row.state === 'unconfigured';
          const envVar = typeof row.detail?.env_var === 'string' ? row.detail.env_var : '';
          const canSeal = row.operator_action_code === 'set_coordinator_env' && !!envVar;
          const showAction = !!action && (row.state === 'blocked' || row.state === 'unconfigured') && !optionalGate && !canSeal;
          return (
            <li
              key={row.capability} data-capability={row.capability} data-state={row.state}
              data-execution-verified={tone === null ? undefined : tone === 'ready' ? 'true' : 'false'}
              className="cap-pill inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2.5 py-1 text-xs"
              title={action || row.reason || undefined}
            >
              <span className="cap-dot h-1.5 w-1.5 shrink-0 rounded-full" aria-hidden="true" />
              <span className="font-medium text-ink">{label}</span>
              <span className="text-muted">{state}</span>
              {canSeal ? (
                <button type="button" className="cap-seal rounded border border-line px-1.5 py-0.5 text-warn" data-env-var={envVar} onClick={() => onSealEnv(envVar)}>
                  {t('setup.saveCoordinatorEnv')}
                </button>
              ): null}
              {showAction ? <span className="cap-action text-warn">· {action}</span>: null}
              {optionalGate ? <span className="text-muted">· {t('setup.optionalGate')}</span>: null}
            </li>
);
        })}
      </ul>
      {envRows.length ? (
        <div id="coordinator-env" className="mt-2">
          <p className="text-xs font-medium text-ink">{t('setup.coordinatorEnvTitle')}</p>
          <ul className="mt-1 flex flex-col gap-1">
            {envRows.map((env) => (
              <li key={env.name} data-env={env.name} data-configured={env.configured ? 'true': 'false'} data-sealed={env.sealed ? 'true': 'false'} className="flex flex-wrap items-center gap-2 text-xs">
                <code>{env.name}</code>
                <span className="text-muted">{env.sealed ? t('setup.coordinatorEnvSet'): env.configured ? t('setup.coordinatorEnvProcess'): t('setup.coordinatorEnvMissing')}</span>
                <button type="button" id={'coordinator-env-open-' + env.name} className="rounded border border-line px-1.5 py-0.5" onClick={() => onSealEnv(env.name)}>
                  {env.sealed ? t('setup.updateCoordinatorEnv'): t('setup.saveCoordinatorEnv')}
                </button>
                {env.sealed ? (
                  <button type="button" className="text-warn" onClick={() => onClearEnv(env.name)}>{t('setup.clearCoordinatorEnv')}</button>
                ): null}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-muted">{t('setup.coordinatorEnvHint')}</p>
        </div>
      ): null}
      </details>
    </section>
);
}

// The pending setup requests (needs_approval / needs_secret / configuring), shown
// above the chat like the old setup-panel. The capability status strip is not
// part of this surface; StatusStrip hosts that same projection.
export function SetupPanel({store}: {store: Store}) {
  const {t} = useTranslation();
  const setup = store.snapshot!.setup;
  const requests = (setup.requests || []).filter((r) => ['needs_approval', 'needs_secret', 'configuring'].includes(r.status));
  const [secretReq, setSecretReq] = useState<{id: string; target: SecretTarget} | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const onboarding = !setup.model.configured || requests.length > 0;
  if (!onboarding) return null;
  return (
    <>
      <section id="setup-panel" className="border-b border-line bg-warn/5 px-5 py-3">
        {!setup.model.configured ? (
          <div className="mini">
            <h3 className="font-medium">{t('setup.connectModelTitle')}</h3>
            <p className="text-sm text-muted">{t('setup.connectModelDesc')}</p>
            <button className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm text-white" onClick={() => setShowSettings(true)}>{t('setup.setupModel')}</button>
          </div>
): null}
        {requests.map((req) => <RequestRow key={req.id} req={req} store={store} onSecret={(target) => setSecretReq({id: req.id, target})} />)}
      </section>
      {showSettings ? <SettingsDialog store={store} onClose={() => setShowSettings(false)} />: null}
      {secretReq ? <SecretDialog store={store} reqId={secretReq.id} target={secretReq.target} onClose={() => setSecretReq(null)} />: null}
    </>
);
}

// The same capability status strip Setup used to render on the home shell.
// App mounts it only on the computers page.
export function StatusStrip({store}: {store: Store}) {
  const {t} = useTranslation();
  const setup = store.snapshot!.setup;
  const capabilities = setup.capabilities || [];
  const envRows = setup.coordinator_env || [];
  const [envName, setEnvName] = useState<string | null>(null);
  const clearEnv = async (name: string) => {
    if (!confirm(t('setup.clearCoordinatorEnvConfirm', {var: name}))) return;
    try { await api('setup/coordinator-env/clear', {name}); await store.refresh(); }
    catch (e: unknown) { store.setNotice(friendlyError(e)); }
  };
  if (!capabilities.length && !envRows.length) return null;
  return (
    <>
      <CapabilityList rows={capabilities} envRows={envRows} onSealEnv={setEnvName} onClearEnv={(name) => void clearEnv(name)} />
      {envName ? <CoordinatorEnvDialog store={store} name={envName} onClose={() => setEnvName(null)} />: null}
    </>
);
}

function RequestRow({req, store, onSecret}: {req: SetupRequest; store: Store; onSecret: (t: SecretTarget) => void}) {
  const {t} = useTranslation();
  const [endpoint, setEndpoint] = useState('');
  const [fault, setFault] = useState('');
  const p = (req.proposal || {}) as Record<string, any>;
  const asksEndpoint = p.action === 'configure' || (p.action === 'executor_config' && !p.installation_id);
  const declaring = p.action === 'declare_adapter';
  const host = store.snapshot!.hosts.find((h) => h.id === req.host_id);
  const command = Array.isArray(p.command) ? p.command.map(String).join(' ') : '';
  const showFault = (error: unknown) => {
    const text = friendlyError(error) || t('setup.operationFailed');
    setFault(text);
    store.setNotice(text);
  };
  return (
    <div className="mini mt-2 rounded-lg border border-line bg-raised p-3">
      <h3 className="font-medium">{req.status === 'needs_secret' ? t('setup.needsSecretTitle'): req.status === 'configuring' ? t('setup.configuringTitle'): t('setup.confirmSetupTitle')}</h3>
      <p className="text-sm text-muted">{host?.name || req.host_id}</p>
      {req.status === 'needs_approval' ? (
        <>
          {declaring ? (
            <>
              <p className="text-sm">{t('setup.declareAdapterLine', {adapter: p.adapter})}</p>
              <p className="text-xs text-warn">{t('setup.declareAdapterBoundary')}</p>
              <p className="text-xs">{t('setup.commandLine', {command})}</p>
              <pre className="mt-1 overflow-auto text-xs">{JSON.stringify(p.adapter_row, null, 2)}</pre>
            </>
          ) : (
            <>
          <p className="text-sm">{t('setup.requestActionLine', {action: p.action === 'configure' ? t('setup.actionConnectCli') : p.action === 'executor_config' ? t('setup.actionExecutorConfig') : t('setup.actionRemoveKey'), agent: p.adapter || p.installation_id})}</p>
          <p className="text-xs">{t('setup.cwdLine', {cwd: p.cwd || t('setup.cwdDefault')})}</p>
          <p className="text-xs">{t('setup.authLine', {auth: p.auth === 'api_key' ? t('setup.authApiKey'): p.auth === 'existing' ? t('setup.authExisting'): p.auth || t('setup.authUnregistered')})}</p>
          {p.draft ? <p className="mt-1 text-xs">{t('setup.draftLine', {draft: JSON.stringify({
            profile: p.draft.profile, env: p.draft.env, extra_argv: p.draft.extra_argv, cwd: p.draft.cwd,
            credential_ids: p.draft.secret_refs, command: p.command,
          })})}</p>: null}
          {p.uses_qwen ? <p className="text-xs">{t('setup.sharedQwen')}</p>: null}
            </>
          )}
          {asksEndpoint && !declaring ? (
            <>
              <p className="mt-1 text-xs text-warn">{t('setup.securityBoundary')}</p>
              <label className="mt-1 block text-sm">{t('setup.apiEndpointOptional')}<input type="url" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1" /></label>
            </>
): null}
          <div className="mt-2 flex gap-2">
            <button className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white" onClick={async () => {
              setFault('');
              try { await api('setup/' + req.id + '/approve', asksEndpoint ? {endpoint: endpoint.trim()}: {}); await store.refresh(); }
              catch (e: unknown) { showFault(e); }
            }}>{t('setup.applySettings')}</button>
            <button className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={async () => {
              setFault('');
              try { await api('setup/' + req.id + '/deny', {}); await store.refresh(); }
              catch (e: unknown) { showFault(e); }
            }}>{t('setup.denySettings')}</button>
          </div>
        </>
): null}
      {req.status === 'needs_secret' ? (
        <button className="mt-2 rounded-lg bg-brand px-3 py-1.5 text-sm text-white" onClick={async () => {
          setFault('');
          try { const target = await api<SecretTarget>('setup/' + req.id + '/secret-target'); onSecret(target); }
          catch (e: unknown) { showFault(e); }
        }}>{t('setup.openSecretWindow')}</button>
): null}
      {fault ? <p className="setup-fault mt-2 text-xs text-warn" role="alert">{fault}</p>: null}
    </div>
);
}

function CoordinatorEnvDialog({store, name, onClose}: {store: Store; name: string; onClose: () => void}) {
  const {t} = useTranslation();
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  return (
    <Modal id="coordinator-env-dialog" title={t('setup.coordinatorEnvDialogTitle')} onClose={() => { setValue(''); onClose(); }} className="secret-modal">
      <form id="coordinator-env-form" onSubmit={async (e) => {
        e.preventDefault();
        const secret = value;
        setValue('');
        setSaving(true);
        setError('');
        try {
          const target = await api<SecretTarget>('setup/coordinator-env/target?name=' + encodeURIComponent(name));
          const sealed = await sealCredential(secret, target);
          await api('setup/coordinator-env', {name, sealed});
          onClose();
          await store.refresh();
        } catch (err: unknown) {
          setError(friendlyError(err));
          setSaving(false);
        }
      }}>
        <h2 className="text-lg font-semibold">{t('setup.coordinatorEnvDialogTitle')}</h2>
        <p id="coordinator-env-purpose" className="mt-1 text-sm text-muted">{t('setup.coordinatorEnvPurpose', {var: name})}</p>
        <label className="mt-3 block text-sm">{t('setup.secret')}<input id="coordinator-env-secret" type="password" autoComplete="new-password" required value={value} onChange={(e) => setValue(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <p className="mt-1 text-xs text-muted">{t('setup.coordinatorEnvStorageHint')}</p>
        {error ? <p id="coordinator-env-error" role="alert" className="mt-1 text-xs text-warn">{error}</p>: null}
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" id="coordinator-env-close" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={() => { setValue(''); onClose(); }}>{t('setup.cancel')}</button>
          <button id="coordinator-env-save" className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={saving}>{t('setup.encryptAndSave')}</button>
        </div>
      </form>
    </Modal>
  );
}

function SecretDialog({store, reqId, target, onClose}: {store: Store; reqId: string; target: SecretTarget; onClose: () => void}) {
  const {t} = useTranslation();
  const [value, setValue] = useState('');
  return (
    <Modal id="secret-dialog" title={t('setup.secretDialogTitle')} onClose={() => { setValue(''); onClose(); }} className="secret-modal">
      <form id="secret-form" onSubmit={async (e) => {
        e.preventDefault();
        const v = value; setValue('');
        try { const sealed = await sealCredential(v, target); await api('setup/' + reqId + '/secret', {sealed}); onClose(); await store.refresh(); }
        catch (err: any) { store.setNotice(err?.message || t('setup.saveFailed')); }
      }}>
        <h2 className="text-lg font-semibold">{t('setup.secretSaveHeading')}</h2>
        <p id="secret-purpose" className="mt-1 text-sm text-muted">{target.host_id} · {target.installation_id} · {target.endpoint || t('setup.cliDefaultEndpoint')} · {target.storage}</p>
        <label className="mt-3 block text-sm">{t('setup.secret')}<input id="device-secret" type="password" autoComplete="new-password" required value={value} onChange={(e) => setValue(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5" /></label>
        <p className="mt-1 text-xs text-muted">{t('setup.secretStorageHint')}</p>
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" id="secret-close" className="rounded-lg border border-line px-3 py-1.5 text-sm" onClick={() => { setValue(''); onClose(); }}>{t('setup.cancel')}</button>
          <button id="secret-save" className="rounded-lg bg-brand px-3 py-1.5 text-sm text-white">{t('setup.encryptAndSave')}</button>
        </div>
      </form>
    </Modal>
);
}
