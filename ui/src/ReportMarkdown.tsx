/* Render common report formatting as React elements without executing source HTML.
   Only references resolved by the caller become passage controls. Unsupported
   syntax stays readable as text; the saved report is never changed. */
import type { ReactNode } from 'react';

type SourceReference = { sourceId: string; spanId: string; label: string };
type Props = {
  text: string;
  sourceReferences?: SourceReference[];
  onOpenSourceReference?: (sourceId: string, spanId: string) => void;
};

function safeUrl(value: string) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function tableCells(line: string) {
  const cells: string[] = [];
  let cell = '', code = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '\\' && line[index + 1] === '|') { cell += '|'; index += 1; }
    else if (character === '`') { code = !code; cell += character; }
    else if (character === '|' && !code) { cells.push(cell.trim()); cell = ''; }
    else cell += character;
  }
  cells.push(cell.trim());
  if (line.trim().startsWith('|')) cells.shift();
  if (line.trim().endsWith('|')) cells.pop();
  return cells;
}

const listLine = (line: string) => /^(\s*)([-+*]|\d+[.)])\s+(.+)$/.exec(line);
const tableDivider = (line: string) => line.includes('|') && tableCells(line).every(cell => /^:?-{3,}:?$/.test(cell));
const headingLine = (line: string) => /^ {0,3}(#{1,6})\s+(.+?)(?:[ \t]+#+)?[ \t]*$/.exec(line);

export function ReportMarkdown({ text, sourceReferences = [], onOpenSourceReference }: Props) {
  const references = new Map(sourceReferences.map(ref => [`${ref.sourceId}::${ref.spanId}`, ref]));
  const escaped = [...references.keys()].sort((a, b) => b.length - a.length).map(key => key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const referencePattern = escaped.length ? new RegExp(`(?<![\\w.:-])(${escaped.join('|')})(?![\\w:-]|\\.[\\w.-])`, 'g') : null;

  function plain(value: string): ReactNode {
    if (!referencePattern) return value;
    return value.split(referencePattern).map((part, index) => {
      const ref = references.get(part);
      if (!ref) return part;
      return onOpenSourceReference
        ? <button className="inline-citation" type="button" key={index} aria-label={`Open cited passage ${ref.label}`} onClick={() => onOpenSourceReference(ref.sourceId, ref.spanId)}>[{ref.label}]</button>
        : <span key={index}>[{ref.label}]</span>;
    });
  }

  function inline(value: string): ReactNode[] {
    const nodes: ReactNode[] = [];
    let start = 0;
    for (let index = 0; index < value.length;) {
      const rest = value.slice(index);
      const code = /^`([^`\n]+)`/.exec(rest);
      const bracketed = /^\[([^\]\n]+)\](?!\()/.exec(rest);
      const sourceReference = bracketed && references.has(bracketed[1]) ? bracketed : null;
      const strong = /^(?:\*\*([^\n]+?)\*\*|__([^\n]+?)__)/.exec(rest);
      const emphasis = /^(?:\*([^*\n]+)\*|_([^_\n]+)_)/.exec(rest);
      const link = value[index - 1] !== '!' ? /^\[([^\]\n]+)\]\(([^\s)]+)\)/.exec(rest) : null;
      const match = code ?? sourceReference ?? strong ?? emphasis ?? link;
      if (!match) { index += 1; continue; }
      if (index > start) nodes.push(plain(value.slice(start, index)));
      if (code) nodes.push(references.has(code[1]) ? <span key={index}>{plain(code[1])}</span> : <code key={index}>{code[1]}</code>);
      else if (sourceReference) nodes.push(<span key={index}>{plain(sourceReference[1])}</span>);
      else if (strong) nodes.push(<strong key={index}>{plain(strong[1] ?? strong[2])}</strong>);
      else if (emphasis) nodes.push(<em key={index}>{plain(emphasis[1] ?? emphasis[2])}</em>);
      else if (link) {
        const href = safeUrl(link[2]);
        nodes.push(href ? <a key={index} href={href} target="_blank" rel="noopener noreferrer">{link[1]}</a> : match[0]);
      }
      index += match[0].length;
      start = index;
    }
    if (start < value.length) nodes.push(plain(value.slice(start)));
    return nodes;
  }

  function blocks(lines: string[]): ReactNode[] {
    const nodes: ReactNode[] = [];
    for (let index = 0; index < lines.length;) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const key = index;
      const fence = /^\s*(`{3,}|~{3,})/.exec(line);
      if (fence) {
        const body: string[] = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith(fence[1])) body.push(lines[index++]);
        if (index < lines.length) index += 1;
        nodes.push(<pre key={key}><code>{body.join('\n')}</code></pre>);
        continue;
      }
      const heading = headingLine(line);
      if (heading) {
        // Reports live beneath the card title; keep their headings in that hierarchy.
        const Heading = `h${Math.min(6, heading[1].length + 3)}` as 'h4' | 'h5' | 'h6';
        nodes.push(<Heading key={key}>{inline(heading[2])}</Heading>); index += 1; continue;
      }
      if (index + 1 < lines.length && line.includes('|') && tableDivider(lines[index + 1])) {
        const headings = tableCells(line);
        const rows: string[][] = [];
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) rows.push(tableCells(lines[index++]));
        nodes.push(<div className="report-table-scroll" key={key}><table><thead><tr>{headings.map((cell, cellIndex) => <th scope="col" key={cellIndex}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>);
        continue;
      }
      const firstItem = listLine(line);
      if (firstItem) {
        const ordered = /^\d/.test(firstItem[2]);
        const indentation = firstItem[1].length;
        const items: string[][] = [];
        while (index < lines.length) {
          const item = listLine(lines[index]);
          if (!item || item[1].length !== indentation || /^\d/.test(item[2]) !== ordered) break;
          const content = [item[3]]; index += 1;
          while (index < lines.length && lines[index].trim() && (lines[index].match(/^\s*/)?.[0].length ?? 0) > indentation) {
            const spaces = lines[index].match(/^\s*/)?.[0].length ?? 0;
            content.push(lines[index++].slice(Math.min(indentation + 2, spaces)));
          }
          items.push(content);
        }
        const List = ordered ? 'ol' : 'ul';
        nodes.push(<List key={key} start={ordered ? Number.parseInt(firstItem[2]) : undefined}>{items.map((item, itemIndex) => <li key={itemIndex}>{blocks(item)}</li>)}</List>);
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quote: string[] = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*> ?/, ''));
        nodes.push(<blockquote key={key}>{blocks(quote)}</blockquote>); continue;
      }
      const paragraph = [line]; index += 1;
      while (index < lines.length && lines[index].trim() && !headingLine(lines[index]) && !listLine(lines[index]) && !/^\s*(`{3,}|~{3,}|>)/.test(lines[index]) && !(index + 1 < lines.length && tableDivider(lines[index + 1]))) paragraph.push(lines[index++]);
      nodes.push(<p key={key}>{inline(paragraph.join('\n'))}</p>);
    }
    return nodes;
  }

  return <div className="report-markdown">{blocks(text.replaceAll('\r\n', '\n').split('\n'))}</div>;
}
