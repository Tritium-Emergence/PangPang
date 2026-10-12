import {useRef} from 'react';
import {useTranslation} from 'react-i18next';
import {api} from '../api';
import type {Store} from '../store';
import type {Task} from '../types';

// Once / always / single approve. Disabled only while the POST is in flight;
// the card leaves when the server status changes.
export function ApprovalButtons({task, store, single, compact}: {task: Task; store: Store; single: string; compact?: boolean}) {
  const {t} = useTranslation();
  const required = task.installation_authorization === 'required';
  const cls = (compact ? 'rounded px-2 py-1 text-xs' : 'rounded-lg px-3 py-1.5 text-sm') + ' bg-brand text-white';
  const post = (always?: true) => api(`tasks/${task.id}/approve`, always
    ? {spec_digest: task.spec_digest, always} : {spec_digest: task.spec_digest});
  return <>
    <Once label={required ? t('chat.approveOnce') : single} className={cls} run={() => post()} store={store} />
    {required ? <Once label={t('chat.approveAlways')} className={cls} run={() => post(true)} store={store} /> : null}
  </>;
}

function Once({label, className, run, store}: {label: string; className: string; run: () => Promise<unknown>; store: Store}) {
  const {t} = useTranslation();
  const busy = useRef(false);
  return <button className={className} onClick={async (e) => {
    const btn = e.currentTarget;
    if (busy.current) return;
    busy.current = true; btn.disabled = true;
    try { await run(); store.setNotice(null); await store.refresh(); }
    catch (err: any) { store.setNotice(err?.message || t('chat.operationFailed')); btn.disabled = false; }
    finally { busy.current = false; }
  }}>{label}</button>;
}
