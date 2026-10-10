// Markdown is rendered via React elements built from a tiny tokenizer — never
// dangerouslySet raw HTML (XSS). The security-sensitive pure helpers safeUrl/unwrapFrame
// live in pure.ts (the single source the Layer-B node tests lock).
import React from 'react';
import {safeUrl, unwrapFrame} from './pure';
import {ZoomImage} from './components/MediaPreview';

export {safeUrl, unwrapFrame};

// Keys are deterministic PER RENDER: a counter seeded at 0 each time Markdown() runs,
// so the same source yields the same keys on every render (React reconciles in place
// and never rebuilds the bubble interior — scroll/identity stability). A module-global
// counter would mint fresh keys every render and defeat that.
function inline(text: string, k: () => string): React.ReactNode[] {
  const re = /!\[([^\]]*)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|_([^_]+)_/g;
  const out: React.ReactNode[] = [];
  let last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) { const u = safeUrl(m[2]); out.push(u ? <ZoomImage key={k()} className="md-img max-w-full" src={u} alt={m[1]} />: m[0]); }
    else if (m[3] !== undefined) { const u = safeUrl(m[4]); out.push(u ? <a key={k()} href={u} target="_blank" rel="noopener noreferrer">{m[3]}</a>: m[3]); }
    else if (m[5] !== undefined) out.push(<code key={k()}>{m[5]}</code>);
    else if (m[6] !== undefined) out.push(<strong key={k()}>{m[6]}</strong>);
    else if (m[7] !== undefined) out.push(<em key={k()}>{m[7]}</em>);
    else if (m[8] !== undefined) out.push(<em key={k()}>{m[8]}</em>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Split on UNESCAPED '|' without a lookbehind assertion. Vite's default build target
// includes safari14, which has no lookbehind, so esbuild lowers a /(?<!\\)\|/ literal to
// new RegExp("(?<!\\\\)\\|") — that parses, then THROWS SyntaxError when constructed on
// Safari < 16.4. With no error boundary in this app the throw unmounts the React root,
// so any message holding two consecutive '|' lines blanked the whole UI on older iOS.
// U+0001 stands in for an escaped pipe while splitting; it carries no meaning in Bot
// prose, and the worst case if one ever arrived is that it renders as '|'.
const cells = (line: string) => line.trim().replace(/^\||\|$/g, '').replace(/\\\|/g, '\u0001').split('|').map((x) => x.trim().replace(/\u0001/g, '|'));
const tableAt = (lines: string[], i: number) => lines[i]?.includes('|') && lines[i + 1]?.includes('|') && cells(lines[i + 1]).every((x) => /^:?-{3,}:?$/.test(x));
const hr = (line: string) => /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);

// Minimal block markdown → React (headings, code fence, quote, list, paragraph).
export function Markdown({src}: {src: string}) {
  let seq = 0;
  const k = () => 'md' + (seq++); // per-render deterministic keys
  const lines = String(src ?? '').replace(/\r\n/g, '\n').split('\n');
  const blocks: React.ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      i++; const buf: string[] = [];
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; blocks.push(<pre key={k()} className="md-pre overflow-auto rounded-lg bg-surface p-3 text-xs"><code>{buf.join('\n')}</code></pre>);
      continue;
    }
    if (hr(line)) { blocks.push(<hr key={k()} className="md-hr" />); i++; continue; }
    if (tableAt(lines, i)) {
      const headers = cells(line), align = cells(lines[i + 1]); i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(cells(lines[i++]));
      const cls = (n: number) => align[n]?.startsWith(':') && align[n]?.endsWith(':') ? 'text-center': align[n]?.endsWith(':') ? 'text-right': 'text-left';
      blocks.push(<div key={k()} className="md-table-scroll"><table className="md-table"><thead><tr>{headers.map((v, n) => <th key={k()} className={cls(n)}>{inline(v, k)}</th>)}</tr></thead><tbody>{rows.map((row) => <tr key={k()}>{headers.map((_, n) => <td key={k()} className={cls(n)}>{inline(row[n] || '', k)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { const L = Math.min(h[1].length + 1, 5); blocks.push(React.createElement('h' + L, {key: k(), className: 'md-h font-semibold'}, inline(h[2], k))); i++; continue; }
    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
      blocks.push(<blockquote key={k()} className="md-quote border-l-2 border-line pl-3 text-muted">{inline(buf.join('\n'), k)}</blockquote>);
      continue;
    }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line); const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) { items.push(<li key={k()}>{inline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''), k)}</li>); i++; }
      blocks.push(React.createElement(ordered ? 'ol': 'ul', {key: k(), className: 'md-list ml-5 list-disc'}, items));
      continue;
    }
    const buf = [line]; i++;
    while (i < lines.length && lines[i].trim() && !hr(lines[i]) && !tableAt(lines, i) && !/^(#{1,4}\s|```|\s*>|\s*([-*+]|\d+\.)\s)/.test(lines[i])) { buf.push(lines[i]); i++; }
    blocks.push(<p key={k()} className="md-p">{inline(buf.join('\n'), k)}</p>);
  }
  return <>{blocks}</>;
}
