/* Render a small, safe subset of formatting found in saved agent text.
   This is display-only: the original text remains the authoritative record. */
import type { ReactNode } from 'react';

const labels = [
  { marker: '**Citations:**', title: 'Citations' },
  { marker: '**Limitations:**', title: 'Limitations' },
  { marker: '**Limitation:**', title: 'Limitation' },
] as const;

type Part = { title?: string; text: string };

function sectionParts(paragraph: string): Part[] {
  const parts: Part[] = [];
  let title: string | undefined;
  let start = 0;
  for (let index = 0; index < paragraph.length;) {
    if (paragraph[index] === '`') {
      const closing = paragraph.indexOf('`', index + 1);
      if (closing > index + 1) {
        index = closing + 1;
        continue;
      }
    }
    const label = (index === 0 || /\s/.test(paragraph[index - 1]))
      ? labels.find(item => paragraph.startsWith(item.marker, index))
      : undefined;
    if (!label) {
      index += 1;
      continue;
    }
    if (index > start || title) parts.push({ title, text: paragraph.slice(start, index) });
    title = label.title;
    index += label.marker.length;
    start = index;
  }
  parts.push({ title, text: paragraph.slice(start) });
  return parts;
}

function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let plain = '';
  let index = 0;
  const flush = () => {
    if (plain) nodes.push(plain);
    plain = '';
  };
  while (index < text.length) {
    if (text.startsWith('**', index)) {
      const closing = text.indexOf('**', index + 2);
      const content = closing >= 0 ? text.slice(index + 2, closing) : '';
      if (content && !content.includes('\n')) {
        flush();
        nodes.push(<strong key={index}>{content}</strong>);
        index = closing + 2;
        continue;
      }
    }
    if (text[index] === '`') {
      const closing = text.indexOf('`', index + 1);
      const content = closing >= 0 ? text.slice(index + 1, closing) : '';
      if (content && !content.includes('\n')) {
        flush();
        nodes.push(<code key={index}>{content}</code>);
        index = closing + 1;
        continue;
      }
    }
    if (text[index] === '\r' && text[index + 1] === '\n') {
      flush();
      nodes.push(<br key={index} />);
      index += 2;
      continue;
    }
    if (text[index] === '\n') {
      flush();
      nodes.push(<br key={index} />);
      index += 1;
      continue;
    }
    plain += text[index];
    index += 1;
  }
  flush();
  return nodes;
}

export function RecordedText({ text }: { text: string }) {
  const paragraphs = text.split(/\r?\n\s*\r?\n/);
  return <div className="recorded-text">{paragraphs.flatMap((paragraph, paragraphIndex) =>
    sectionParts(paragraph).map((part, partIndex) => part.title
      ? <section className="recorded-text-section" aria-label={part.title} key={paragraphIndex + '-' + partIndex}>
        <h5>{part.title}</h5><p>{inline(part.text)}</p>
      </section>
      : <p key={paragraphIndex + '-' + partIndex}>{inline(part.text)}</p>)
  )}</div>;
}
