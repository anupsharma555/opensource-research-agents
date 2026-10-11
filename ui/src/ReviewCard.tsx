/* Keep a review card's heading and context visible while its body is folded.
   Controlled expansion lets the review preserve focus choices across tabs.
   Hidden bodies remain mounted so nested source disclosures retain their state. */
import type { ReactNode } from 'react';

type Props = {
  as?: 'section' | 'article' | 'div';
  id: string;
  label: string;
  controlLabel: string;
  className?: string;
  eyebrow: string;
  title: string;
  summary?: ReactNode;
  actions?: ReactNode;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
};

export function ReviewCard({ as: Tag = 'section', id, label, controlLabel, className = 'panel', eyebrow, title, summary, actions, expanded, onToggle, children }: Props) {
  return <Tag id={id} tabIndex={-1} className={`${className} review-card`} aria-label={label}>
    <div className="review-card-header">
      <button type="button" className="review-card-toggle" aria-label={`${expanded ? 'Collapse' : 'Expand'} ${controlLabel}`} title={`${expanded ? 'Collapse' : 'Expand'} ${controlLabel}`} aria-expanded={expanded} aria-controls={`${id}-body`} onClick={onToggle}><span aria-hidden="true">▸</span></button>
      <div className="review-card-heading"><span className="eyebrow">{eyebrow}</span><h3>{title}</h3>{summary && <div className="review-card-summary">{summary}</div>}</div>
      {actions && <div className="review-card-actions">{actions}</div>}
    </div>
    <div id={`${id}-body`} className="review-card-body" hidden={!expanded}>{children}</div>
  </Tag>;
}
