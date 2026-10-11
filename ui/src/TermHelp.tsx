/* Define research-workspace terms beside their labels without hiding the work.
   Definitions can be opened by pointer, keyboard focus, or a button press. */
import { useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

const stepDefinition = 'One saved step by one agent on one task in a run. It may include preparing a model request, using a tool, giving an answer or waiting. It is not a separate research run. Technical records call it an attempt.';
const hostDefinition = 'The software that saves the research work and manages when model calls and tools can run. Agents make the scientific judgments; the host records and coordinates their actions.';

export const atlasTerms = {
  run: 'One research request and the work done to answer it, including its tasks, messages, sources and reports. A run keeps the same ID through waits and restarts. A new follow-up has its own run ID.',
  task: 'A piece of work within a run, such as finding a paper or checking an answer. Agents may take several steps to carry it out. One run can contain several tasks.',
  step: stepDefinition,
  'work step': stepDefinition,
  host: hostDefinition,
  system: hostDefinition,
  'prepared request': 'An input assembled for a model call. It may still be waiting or may be replaced before it is sent. Prepared does not mean sent, and does not prove that tokens were used.',
  tool: 'An action an agent can ask the software to perform, such as searching for a paper, reading saved text or sending a message. A tool result is not the final research answer.',
  agent: 'An AI participant in the research run. Its name identifies who did a step or sent a message. The task describes the work it is doing; the name does not limit it to one role.',
  peer: 'Another research agent that can be asked to help or review work. Saving a request, delivering it, receiving a reply and reviewing that reply are separate events.',
  'earlier run': 'The saved research run that this follow-up continues. Its question, work and answer stay separate from the new work. Technical records call it the parent run.',
  'shared work board': 'The run’s common list of proposed work. Agents can inspect items, take responsibility, add results or challenge a proposal. A board entry does not establish that a message was sent to another agent.',
  'source copy': 'The exact saved version of retrieved or supplied source material. Displayed passages may cover only part of it. A source copy is not necessarily a separate study.',
  'accepted report': 'An answer saved as a report for this run. This does not mean its scientific claims have been independently checked or that every task is finished. Read its evidence and limitations.',
  'model reasoning': 'Reasoning text actually returned by the selected model for a particular call, when available and permitted. It is separate from the answer and messages sent to peers. The viewer does not reconstruct missing reasoning.',
  'task assignment': 'The work an agent was asked to perform. Naming another agent in an assignment does not establish that the first agent sent it a message.',
} as const;

export function TermHelp({ term }: { term: keyof typeof atlasTerms }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const tooltip = useRef<HTMLSpanElement>(null);
  const [position, setPosition] = useState({ left: 12, top: 12 });
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = button.current?.getBoundingClientRect();
      const bounds = tooltip.current?.getBoundingClientRect();
      if (!anchor || !bounds) return;
      setPosition({
        left: Math.max(12, Math.min(anchor.left, window.innerWidth - bounds.width - 12)),
        top: anchor.bottom + bounds.height + 20 < window.innerHeight ? anchor.bottom + 8 : Math.max(12, anchor.top - bounds.height - 8),
      });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open]);
  return <span className="term-help" onMouseEnter={() => setOpen(true)} onMouseLeave={event => {
    if (!event.currentTarget.contains(document.activeElement)) setOpen(false);
  }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button ref={button} type="button" className="term-help-button" aria-label={`What does ${term} mean?`} aria-expanded={open} aria-controls={open ? id : undefined} aria-describedby={open ? id : undefined} onFocus={() => setOpen(true)} onClick={() => setOpen(true)} onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); event.stopPropagation(); } }}>i</button>
    {open && createPortal(<span ref={tooltip} id={id} role="tooltip" className="term-help-definition" style={position}>{atlasTerms[term]}</span>, document.body)}
  </span>;
}

export function AtlasTerms() {
  return <details className="inline-detail atlas-terms"><summary>What the workspace terms mean</summary><dl>{Object.entries(atlasTerms).filter(([term]) => !['work step', 'system'].includes(term)).map(([term, meaning]) => <div key={term}><dt>{term === 'step' ? 'step / work step' : term === 'host' ? 'host / system' : term}</dt><dd>{meaning}</dd></div>)}</dl></details>;
}
