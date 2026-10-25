import {useEffect, useRef, useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Store} from '../store';
import {api} from '../api';
import {FileDialog} from './FileDialog';
import {friendlyError} from '../display';
import {sendKeyFor, clearPending} from '../idempotency';
import {restoreComposerDraft} from '../pure';

// Composer. Preserves the selectors/behaviors:
// - #composer form, #message textbox (role=textbox), #send button "发送 →"
// - data-testid="continue-target" shows the one-shot "继续此 Work" target
// - stream_id (stable per bot×tab), work_hint_id (last work), continue_work_id (one-shot)
// - attachments + continuation target belong to the current bot; both clear on bot
// switch — the store does that in select(), so this component just reads props.
// The user bubble and an empty field appear before the network returns. The bubble
// leaves when the durable message arrives, or the draft is put back if the send fails.
// The button stays disabled until that request settles.

function streamId(botId: string): string {
  const key = 'conversationStreamId:' + botId;
  let id = sessionStorage.getItem(key);
  if (!id) { id = crypto.randomUUID(); sessionStorage.setItem(key, id); }
  return id;
}
const workKey = (botId: string) => 'currentWorkId:' + botId;
// last-seen event id per bot×stream: the id of the newest message this tab has
// rendered. The server uses it (with stream_id) to establish strict adjacency for
// correction lineage (service._stream_context). Mirrors the old app.js lastSeenKey.
const lastSeenKey = (botId: string) => 'lastSeenEventId:' + botId + ':' + streamId(botId);

// Deterministic continuation errors that mean the target is genuinely invalid — only
// these clear the one-shot continueTarget. A transient/network-ambiguous failure must
// NOT clear it (the user's explicit intent survives a retry). Mirrors old app.js.
const CONTINUE_INVALID = new Set(['continue_work_forbidden', 'continue_work_not_continuable', 'invalid_continue_work_id']);

export function Composer({store, botId}: {store: Store; botId: string}) {
  const {t} = useTranslation();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [movePrevious, setMovePrevious] = useState(false);
  const {attached, setAttached, continueTarget, setContinueTarget} = store;
  useEffect(() => { if (!continueTarget) setMovePrevious(false); }, [continueTarget]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileDialog, setFileDialog] = useState(false);
  const [dropping, setDropping] = useState(false);
  const inFlight = useRef(0);
  const [uploads, setUploads] = useState(0);
  const sendingRef = useRef(false);

  const send = async () => {
    if (sendingRef.current) return;
    if (inFlight.current || attached.some((a) => a.uploading)) { store.setNotice(t('composer.uploadingWait')); return; }
    const sentText = text;
    if (!sentText.trim()) return;
    sendingRef.current = true;
    setSending(true);
    const ct = continueTarget; // snapshot the one-shot target for this attempt
    const token = store.selectionToken(); // capture the selection epoch at send start
    const mine = () => store.liveSelected() === botId && store.selectionCurrent(token);
    const sentAttached = attached.slice();
    const sentIds = new Set(sentAttached.map((a) => a.id));
    const pendingId = 'pending-' + crypto.randomUUID();
    // Paint before any await so the bubble and the cleared field are not gated on the network.
    store.pushPendingSend({
      id: pendingId, botId, text: sentText.trim(), created: Date.now() / 1000,
      knownIds: store.messages.map((m) => m.id),
    });
    setText((draft) => draft === sentText ? '': draft);
    setAttached((prev) => prev.filter((a) => !sentIds.has(a.id)));
    try {
      const body: Record<string, unknown> = {text: sentText, refs: sentAttached.map((a) => a.id), stream_id: streamId(botId)};
      const cw = sessionStorage.getItem(workKey(botId));
      if (cw) body.work_hint_id = cw;
      const lastSeen = sessionStorage.getItem(lastSeenKey(botId));
      if (lastSeen) body.last_seen_event_id = lastSeen;
      if (ct) body.continue_work_id = ct.id;
      if (ct && movePrevious) body.correct_to_work_id = ct.id;
      const requestKey = await sendKeyFor(botId, {
        text: sentText, refs: sentAttached.map((a) => a.id), continueWorkId: ct?.id || null,
        correctToWorkId: ct && movePrevious ? ct.id : null,
      }, sessionStorage, crypto.subtle, () => crypto.randomUUID());
      const res = await api<any>(`bots/${botId}/messages`, body, requestKey);
      const resolved = res?.resolved_work_id || res?.work_id;
      // Server-confirmed bookkeeping (per-bot sessionStorage) may complete regardless of
      // which bot is now selected — it is keyed by botId and pollutes no live UI state.
      if (resolved) sessionStorage.setItem(workKey(botId), resolved);
      clearPending(botId, sessionStorage); // confirmed: retire this bot's idempotency key
      // Current-UI ephemeral mutations ONLY if this send still owns the live selection.
      if (mine()) {
        if (res?.route_quality === 'fallback') store.setNotice(t('composer.routeFallback'));
        else if (res?.work_switched) store.setNotice(t('composer.workSwitched'));
        setAttached((prev) => prev.filter((a) => !sentIds.has(a.id)));
        if (ct && store.continueTarget === ct) setContinueTarget(null);
        if (ct) setMovePrevious(false);
        await store.refresh();
      }
    } catch (e: any) {
      store.dropPendingSend(pendingId);
      if (!mine()) return; // switched away mid-send: never touch the new bot's ephemeral state
      // Only a DETERMINISTIC invalid-continuation error clears the one-shot target and
      // retires the key. Transient/ambiguous failures keep both so a retry reuses the key.
      if (ct && CONTINUE_INVALID.has(e?.message) && store.continueTarget === ct) {
        setContinueTarget(null);
        clearPending(botId, sessionStorage);
      }
      store.setNotice(e?.message || t('composer.sendFailed'));
      setText((draft) => restoreComposerDraft({text: draft, attached: []}, {text: sentText, attached: []}).text);
      setAttached((prev) => restoreComposerDraft({text: '', attached: prev}, {text: sentText, attached: sentAttached}).attached);
    } finally {
      // Always re-enable this Composer's own send control. `sending` is local component
      // state (the Composer is keyed per bot and unmounts on switch), not cross-bot
      // ephemeral state, so it must reset unconditionally — gating it on mine() could
      // leave the button stuck disabled.
      sendingRef.current = false;
      setSending(false);
    }
  };

  const upload = async (files: FileList | File[]) => {
    const host = (store.snapshot!.hosts || []).find((h) => h.transport === 'local')?.id;
    if (!host) { store.setNotice(t('composer.noServer')); return; }
    const token = store.selectionToken();
    const mine = () => store.liveSelected() === botId && store.selectionCurrent(token);
    const queue = Array.from(files).filter((f) => {
      if (f.size > 8 * 1024 * 1024) { store.setNotice(t('composer.fileTooLarge', {name: f.name})); return false; }
      return true;
    }).map((f) => ({f, tmp: 'uploading-' + crypto.randomUUID()}));
    // Fence the ENTIRE queue synchronously, including FileReader time and later files.
    inFlight.current += queue.length; setUploads(inFlight.current);
    setAttached((prev) => [...prev,...queue.map(({f, tmp}) => ({id: tmp, name: f.name || t('composer.pastedImage'), uploading: true}))]);
    for (const {f, tmp} of queue) {
      try {
        if (!mine()) return;
        const data = await new Promise<string>((ok, bad) => {
          const r = new FileReader(); r.onload = () => ok(String(r.result).split(',', 2)[1] || '');
          r.onerror = () => bad(new Error('invalid_upload')); r.readAsDataURL(f);
        });
        if (!mine()) return;
        const a = await api<any>(`bots/${botId}/uploads`, {host_id: host, name: f.name || 'pasted.png', data});
        if (!mine()) return;
        const preview = /^image\//.test(f.type) ? `data:${f.type};base64,${data}`: undefined;
        setAttached((prev) => prev.map((x) => x.id === tmp ? {id: a.id, name: a.name || f.name || 'file', preview}: x));
      } catch (e) {
        if (!mine()) return;
        setAttached((prev) => prev.filter((x) => x.id !== tmp));
        store.setNotice(f.name + ': ' + friendlyError(e));
      } finally {
        inFlight.current--; if (mine()) setUploads(inFlight.current);
      }
    }
  };

  return (
    <form
      id="composer"
      data-testid="composer"
      className={`composer-root border-t border-line bg-raised px-5 py-3 ${dropping ? 'dropping': ''}`}
      onPaste={(e) => { const files = Array.from(e.clipboardData.files); if (files.length) { e.preventDefault(); void upload(files); } }}
      onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDropping(true); } }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDropping(false); }}
      onDrop={(e) => { e.preventDefault(); setDropping(false); if (e.dataTransfer.files.length) void upload(e.dataTransfer.files); }}
      onSubmit={(e) => { e.preventDefault(); void send(); }}
    >
      <div className="compose-options flex items-center gap-3 text-xs text-muted">
        <span className="compose-hint">{t('composer.hint')}</span>
      </div>
      {fileDialog ? <FileDialog store={store} botId={botId} onClose={() => setFileDialog(false)} />: null}

      <div id="attached" className={`mt-2 flex-wrap gap-2 ${attached.length ? 'flex': 'hidden'}`}>
        {attached.map((a) => (
          <span key={a.id} className="file-chip inline-flex items-center gap-1 rounded-full border border-line px-2 py-0.5 text-xs">
            {a.preview ? <img className="chip-thumb" src={a.preview} alt="" />: null}
            <span>{a.name}{a.uploading ? t('composer.uploadingSuffix'): ''}</span>
            <button type="button" className="text-muted" aria-label={t('composer.removeFile', {name: a.name})} disabled={a.uploading} onClick={() => setAttached((p) => p.filter((x) => x.id !== a.id))}>×</button>
          </span>
))}
      </div>

      {/* Only render the target when set. A `hidden` attr would be defeated by the
          Tailwind `flex` utility (display:flex overrides the attribute). Playwright
          toBeHidden passes for a not-rendered element; toBeVisible needs it present. */}
      {continueTarget ? (
        <div id="continue-target" data-testid="continue-target" className="mt-2 flex items-center gap-2 rounded-lg bg-surface px-3 py-1.5 text-sm">
          <span className="continue-label">{t('composer.continueTarget', {goal: String(continueTarget.goal || '').slice(0, 80)})}</span>
          <label className="ml-auto flex items-center gap-1 text-xs">
            <input type="checkbox" data-testid="move-previous-message" checked={movePrevious} onChange={(e) => setMovePrevious(e.target.checked)} />
            {t('composer.movePrevious')}
          </label>
          <button type="button" className="notice-x text-muted" onClick={() => { setContinueTarget(null); setMovePrevious(false); }}>×</button>
        </div>
): null}

      <div className="compose-row">
        <span className="compose-files flex text-muted">
          <button type="button" id="upload" aria-label={t('composer.uploadFile')} title={t('composer.uploadFile')} onClick={() => fileRef.current?.click()}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 16V5" />
              <path d="M8 8.5 12 4.5 16 8.5" />
              <path d="M5 14.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3.5" />
            </svg>
          </button>
          <button type="button" id="attach" aria-label={t('composer.referenceDeviceFile')} title={t('composer.referenceDeviceFile')} onClick={() => setFileDialog(true)}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 8.2V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2h-6.1L10.4 6H6a2 2 0 0 0-2 2.2Z" />
            </svg>
          </button>
          <input ref={fileRef} type="file" id="file-pick" multiple hidden onChange={(e) => { if (e.target.files?.length) void upload(e.target.files); e.target.value = ''; }} />
        </span>
        <textarea
          id="message"
          rows={3}
          required
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !(e as any).isComposing) { e.preventDefault(); void send(); } }}
          placeholder={t('composer.placeholder')}
          className="mt-2 w-full resize-none rounded-xl border border-line px-3 py-2"
        />
        <div className="compose-bottom mt-2 flex justify-end">
          <button id="send" className="rounded-lg bg-brand px-4 py-2 font-medium text-white disabled:opacity-50" disabled={sending || uploads > 0}>{t('composer.send')} →</button>
        </div>
      </div>
    </form>
);
}
