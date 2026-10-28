import {useEffect, useState} from 'react';
import type {Message, Snapshot} from '../types';
import {useTranslation} from 'react-i18next';
import {Markdown, unwrapFrame} from '../markdown';
import {MediaPreview} from './MediaPreview';
import {clock, dateTime, exchangeLabel} from '../display';
import {COPY_FEEDBACK_MS, messagePlainText} from '../pure';
import {Value, hasValue} from './Facts';

// One message bubble. Assistant messages carry data-testid="assistant-msg" (the
// streaming→final handoff contract counts these). handoff/result payloads render as
// labelled sections; plain assistant/user text renders as safe markdown.
// Copy writes that plain body. Reply binds the composer to this message's work
// id — the id on the row, with no other candidate.
export function MessageBubble({m, snap, botName, onReply}: {
  m: Message; snap: Snapshot; botName: (id?: string) => string;
  onReply?: (workId: string) => void;
}) {
  const {t} = useTranslation();
  const mine = m.from === 'user';
  const isAssistant = m.kind === 'assistant';
  const structured = ['handoff', 'result', 'execution_result'].includes(m.kind);

  const refs = [...new Set(m.artifact_refs || [])]
.map((r) => (snap.artifacts || []).find((a) => a.id === r))
.filter(Boolean) as NonNullable<Snapshot['artifacts'][number]>[];

  return (
    <div
      className={`bubble ${m.kind} my-2 flex flex-col ${mine ? 'items-end': 'items-start'}`}
      {...(isAssistant ? {'data-testid': 'assistant-msg'}: {})}
      data-kind={m.kind}
    >
      <div className="source text-xs text-muted">{mine ? t('msg.you'): botName(m.from)} {m.created ? <time title={dateTime(m.created)} dateTime={new Date(m.created * 1000).toISOString()}>{clock(m.created)}</time>: null}</div>
      {['peer_out', 'peer', 'peer_result'].includes(m.kind) ? <div className="exchange-label text-xs text-brand">{m.kind === 'peer_out' ? t('msg.sentTo', {name: botName(snap.exchanges.find((x) => x.id === m.exchange_id)?.target_bot_id || m.to)}): t('msg.from', {name: botName(m.from)})}</div>: null}
      <div className={`body mt-1 max-w-[80%] whitespace-pre-wrap rounded-2xl px-4 py-2 shadow-sm ${
        mine ? 'bg-brand text-white': 'bg-raised text-ink'
      }`}>
        {structured ? (
          <StructuredPayload content={m.content} kind={m.kind} botName={botName} />
): typeof m.content === 'string' ? (
          <Markdown src={String(unwrapFrame(m.content))} />
): (
          <pre className="evidence-raw overflow-auto text-xs">{JSON.stringify(m.content, null, 2)}</pre>
)}
      </div>
      {m.exchange_id ? (() => { const ex = snap.exchanges.find((x) => x.id === m.exchange_id); return ex ? <p className="exchange-status text-xs text-muted">{t('msg.exchange')} · {ex.status && exchangeLabel[ex.status] ? t(exchangeLabel[ex.status]): ex.status}</p>: null; })(): null}
      {refs.length ? (
        <div className="msg-media mt-1 flex max-w-[80%] flex-wrap gap-2">
          {refs.map((a) => <MediaPreview key={a.id} artifact={a} />)}
        </div>
): null}
      {isAssistant ? (
        <div className="msg-actions flex max-w-[80%] items-center gap-1">
          <CopyControl text={messagePlainText(m.content)} label={t('msg.copy')} />
          <button
            type="button" data-testid="msg-reply" aria-label={t('msg.reply')}
            className="msg-action text-muted hover:text-ink disabled:opacity-40"
            disabled={!m.work_id}
            onClick={() => { if (m.work_id) onReply?.(m.work_id); }}
          >{t('msg.reply')}</button>
        </div>
): null}
    </div>
);
}

function CopyControl({text, label}: {text: string; label: string}) {
  const [ack, setAck] = useState(0);
  const copied = ack !== 0;
  useEffect(() => {
    if (!ack) return;
    const id = window.setTimeout(() => setAck(0), COPY_FEEDBACK_MS);
    return () => window.clearTimeout(id);
  }, [ack]);
  return (
    <button
      type="button" data-testid="msg-copy" data-copied={copied ? 'true' : 'false'}
      aria-label={label}
      className={`msg-action hover:text-ink ${copied ? 'text-good' : 'text-muted'}`}
      onClick={() => {
        let pending: Promise<void> | undefined;
        try { pending = navigator.clipboard?.writeText(text); } catch { return; }
        if (!pending) return;
        void pending.then(() => setAck((n) => n + 1), () => {});
      }}
    >{copied ? '✓' : label}</button>
  );
}

function parse(content: Message['content']): Record<string, any> | null {
  if (content && typeof content === 'object') return content as Record<string, any>;
  if (typeof content === 'string') { try { return JSON.parse(content); } catch { return null; } }
  return null;
}

function StructuredPayload({content, kind, botName}: {content: Message['content']; kind: string; botName: (id?: string) => string}) {
  const {t} = useTranslation();
  const p = parse(content);
  if (!p || Array.isArray(p) || typeof p !== 'object') return <details><summary>{kind === 'handoff' ? t('msg.handoffDetails'): t('msg.resultEvidence')}</summary><Value value={content} /></details>;
  const labels: Record<string, string> = kind === 'handoff'
    ? {target_bot_id: t('msg.field.targetBot'), goal: t('msg.field.goal'), request: t('msg.field.request'), acceptance: t('msg.field.acceptance'), constraints: t('msg.field.constraints'), facts: t('msg.field.facts'), completed: t('msg.field.completed'), open_questions: t('msg.field.openQuestions'), mode: t('msg.field.mode')}
: {summary: t('msg.field.summary'), result: t('msg.field.result'), execution_result: t('msg.field.executionResult'), artifact_refs: t('msg.field.artifactRefs'), outputs: t('msg.field.outputs'), evidence_refs: t('msg.field.evidenceRefs'), error: t('msg.field.error'), verification_status: t('msg.field.verificationStatus'), source_work_id: t('msg.field.sourceWork')};
  const render = (key: string, value: unknown) => {
    if (key === 'target_bot_id' && typeof value === 'string') return botName(value);
    if (key === 'mode') return value === 'deliver_to_user' ? t('msg.mode.deliverToUser'): value === 'return_to_sender' ? t('msg.mode.returnToSender'): <Value value={value} />;
    if (key === 'verification_status' && value === 'bot_report_not_independent_verification') return t('msg.botReportNotVerified');
    return <Value value={value} />;
  };
  const rest = Object.fromEntries(Object.entries(p).filter(([key, v]) => !Object.prototype.hasOwnProperty.call(labels, key) && hasValue(v)));
  return <div className="handoff-card">
    {Object.entries(labels).map(([key, title]) => hasValue(p[key]) ? <section key={key} data-field={key} className="mt-2 first:mt-0"><h4 className="text-xs font-semibold text-muted">{title}</h4><div className={['constraints', 'open_questions', 'error', 'verification_status'].includes(key) ? 'text-sm text-warn': 'text-sm'}>{render(key, p[key])}</div></section>: null)}
    {hasValue(rest) ? <section className="mt-2"><h4 className="text-xs font-semibold text-muted">{t('msg.field.other')}</h4><Value value={rest} /></section>: null}
  </div>;
}
