import {useTranslation} from 'react-i18next';
import type {Task} from '../types';

// Operator approval card. Derived from the task the server froze.
export function RunPreview({task}: {task: Task}) {
  const {t} = useTranslation();
  const preview = task.run_preview;
  if (task.installation_authorization !== 'required' || !preview) return null;
  const command = (preview.command || []).join(' ');
  const roots = (preview.allowed_roots || []).join(', ');
  const credentials = (preview.credential_ids || []).join(', ');
  return (
    <div className="mt-1 space-y-0.5 text-xs text-muted">
      <p>{t('chat.runDevice', {name: preview.host_name || preview.host_id || '—'})}</p>
      <p>{t('chat.runCommand', {command: command || '—'})}</p>
      <p>{t('chat.runCwd', {cwd: preview.cwd || '—'})}</p>
      <p>{t('chat.runRoots', {roots: roots || '—'})}</p>
      {preview.truncated ? <p>{t('chat.runTruncated')}</p> : null}
      <p>{t('chat.runSupervision', {supervision: preview.supervision || '—', consent: preview.process_group_consent || '—'})}</p>
      <p>{t('chat.runCredentials', {ids: credentials || '—'})}</p>
    </div>
  );
}
