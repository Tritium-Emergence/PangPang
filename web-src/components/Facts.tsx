import {useTranslation} from 'react-i18next';
import {dateTime} from '../display';

// One authority for "will Value render anything". A heading whose body renders nothing
// is a lie about the payload, and `v != null && v !== ''` is not that test: [] and {}
// both pass it and then produce an empty <ul>/<pre>. Section guards and Value itself
// read THIS, so the heading and the body can never disagree.
export const hasValue = (v: unknown): boolean =>
  v != null && v !== '' && (!Array.isArray(v) ? typeof v !== 'object' || Object.keys(v as object).length > 0: v.length > 0);

// Evidence/output ITEMS are objects like {path, bytes, sha256}; showed their
// human-meaningful field on one line rather than dumping JSON into the conversation.
// Scoped to items on purpose: a whole object (the '其他' bag) must keep every field.
const itemText = (v: unknown) => v && typeof v === 'object' && !Array.isArray(v)
  ? [(v as Record<string, unknown>).text, (v as Record<string, unknown>).path].find((x) => typeof x === 'string' && x) as string | undefined
: undefined;

export function Value({value}: {value: unknown}) {
  if (!hasValue(value)) return null;
  if (Array.isArray(value)) return <ul className="ml-4 list-disc">{value.map((v, i) => {
    const t = itemText(v);
    return <li key={i}>{t ? <span>{t}</span>: <Value value={v} />}</li>;
  })}</ul>;
  if (typeof value === 'object') return <pre className="evidence-raw overflow-auto text-xs">{JSON.stringify(value, null, 2)}</pre>;
  return <span>{String(value)}</span>;
}
export function Facts({value, labels}: {value?: Record<string, unknown>; labels: Record<string, string>}) {
  return <div className="facts">{Object.entries(labels).map(([key, label]) => hasValue(value?.[key]) ? <section key={key} className="mt-2"><h4 className="text-xs font-semibold text-muted">{label}</h4><Value value={value![key]} /></section>: null)}</div>;
}
export function Created({at}: {at?: number}) { const {t} = useTranslation(); return at ? <p className="meta-time mt-1 text-xs text-muted">{t('facts.createdAt')} <time dateTime={new Date(at * 1000).toISOString()}>{dateTime(at)}</time></p>: null; }
