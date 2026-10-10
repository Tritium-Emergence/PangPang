import {useState} from 'react';
import {useTranslation} from 'react-i18next';
import type {Artifact} from '../types';
import {mediaKind, safeUrl} from '../pure';
import {Modal} from './Modal';

export function ZoomImage({src, alt, className = '', onError}: {src: string; alt: string; className?: string; onError?: () => void}) {
  const {t} = useTranslation();
  const [open, setOpen] = useState(false);
  const url = safeUrl(src);
  if (!url) return null;
  return <>
    <button type="button" className="image-zoom" aria-label={t('media.zoom', {alt})} onClick={() => setOpen(true)}><img src={url} alt={alt} className={className} loading="lazy" onError={onError} /></button>
    {open ? <Modal id="lightbox" title={t('media.preview', {alt})} onClose={() => setOpen(false)} className="lightbox">
      <button type="button" className="lightbox-close" aria-label={t('media.closePreview')} onClick={() => setOpen(false)}>×</button>
      <img className="lightbox-img" src={url} alt={alt} />
    </Modal>: null}
  </>;
}

// Shared by chat and artifact cards: size fence, lazy loading, error fallback and
// independent download remain identical on both surfaces. No eager full-size fetch.
export function MediaPreview({artifact}: {artifact: Artifact}) {
  const {t} = useTranslation();
  const [failed, setFailed] = useState(false);
  const name = artifact.path.split(/[\\/]/).pop() || 'file';
  const url = `/api/v1/artifacts/${encodeURIComponent(artifact.id)}?download=1`;
  const kind = mediaKind(name), big = artifact.size != null && artifact.size > 8 * 1024 * 1024;
  const download = <a className="media-fallback text-brand" href={url} download={name}>{t('media.download', {name})}</a>;
  if (kind === 'other') return download;
  return <div className={`media-preview ${big ? 'media-toobig': failed ? 'media-error': ''}`}>
    {big || failed ? <p className="p-3 text-sm text-muted">{big ? t('media.tooBig'): t('media.loadFailed')}</p>:
      kind === 'image' ? <ZoomImage src={url} alt={name} className="media-img" onError={() => setFailed(true)} />:
      kind === 'video' ? <video className="media-video" src={url} controls preload="metadata" onError={() => setFailed(true)} />:
      <audio className="media-audio" src={url} controls preload="metadata" onError={() => setFailed(true)} />}
    <div className="media-cap text-xs">{download}</div>
  </div>;
}
