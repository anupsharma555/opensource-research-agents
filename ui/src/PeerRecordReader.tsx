/* Fetch exact peer content only after a reader explicitly asks for it.
   Projection summaries, provider reasoning and mailbox content stay separate. */
import { useEffect, useRef, useState } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { getLivePeerRecord } from './live';
import type { LivePeerRecord, PeerRecordReference } from './live';
import { ReportMarkdown } from './ReportMarkdown';
import './PeerRecordReader.css';

type Props = { runId: string; reference: PeerRecordReference; snapshot: ResearchSnapshot; requestQuestion?: string };
type ReadState = { key: string; value?: LivePeerRecord; error?: string; loading?: boolean };

const fieldName = (value: string) => value.replace(/([a-z])([A-Z])/g, '$1 $2').replaceAll('_', ' ');
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : null;

function ReadableValue({ value }: { value: unknown }) {
  if (value === null) return <span>The record explicitly contains no value (null).</span>;
  if (typeof value === 'string') return <span className="peer-record-text">{value || 'Empty saved text.'}</span>;
  if (typeof value === 'number' || typeof value === 'boolean') return <span>{String(value)}</span>;
  if (Array.isArray(value)) return value.length ? <ol>{value.map((item, index) => <li key={index}><ReadableValue value={item} /></li>)}</ol> : <span>Empty saved list.</span>;
  const fields = object(value);
  if (fields) return Object.keys(fields).length ? <dl className="peer-record-fields">{Object.entries(fields).map(([key, item]) => <div key={key}><dt>{fieldName(key)}</dt><dd><ReadableValue value={item} /></dd></div>)}</dl> : <span>The saved record has no fields.</span>;
  return <span>Saved content was not included in the response.</span>;
}

function AnswerContent({ content }: { content: unknown }) {
  const report = object(content);
  if (!report || typeof report.body !== 'string') return <ReadableValue value={content} />;
  return <div className="peer-record-report">
    {text(report.title) && <h4>{text(report.title)}</h4>}
    <ReportMarkdown text={report.body} />
    {Array.isArray(report.citations) && <section><h5>Saved citations ({report.citations.length})</h5>{report.citations.length ? <ol>{report.citations.map((citation, index) => <li key={index}><ReadableValue value={citation} /></li>)}</ol> : <p>None saved.</p>}</section>}
    {Array.isArray(report.limitations) && <section><h5>Saved limitations</h5>{report.limitations.length ? <ol>{report.limitations.map((item, index) => <li key={index}><ReadableValue value={item} /></li>)}</ol> : <p>None saved.</p>}</section>}
    <details><summary>Exact structured answer</summary><pre>{JSON.stringify(content, null, 2)}</pre></details>
  </div>;
}

function MessageContent({ content }: { content: unknown }) {
  const fields = object(content);
  const question = typeof content === 'string' ? text(content) : text(fields?.question);
  if (question === null) return <ReadableValue value={content} />;
  const remaining = fields ? Object.fromEntries(Object.entries(fields).filter(([key]) => key !== 'question')) : null;
  return <>
    <blockquote className="peer-message-quote">{question}</blockquote>
    {remaining && Object.keys(remaining).length > 0 && <div className="peer-message-other-fields"><ReadableValue value={remaining} /></div>}
  </>;
}

function unavailableReason(reason?: string | null) {
  if (reason === 'not_found' || reason === 'record_not_found' || reason === 'saved_record_unavailable') return 'This exact saved record is unavailable.';
  if (reason === 'not_in_selected_snapshot' || reason === 'outside_selected_run' || reason === 'record_not_in_selected_run') return 'This record is not available within the selected run and its verified history.';
  if (reason === 'saved_lineage_unavailable') return 'The saved history needed to verify this record is unavailable.';
  if (reason === 'content_unavailable' || reason === 'artifact_unavailable' || reason === 'answer_content_unavailable') return 'The exact record is saved, but its content is unavailable.';
  return 'The exact saved content is unavailable. The saved request and status above remain readable.';
}

export function PeerRecordReader({ runId, reference, snapshot, requestQuestion }: Props) {
  const key = `${runId}:${snapshot.snapshotId}:${reference.kind}:${reference.id}:${reference.version}`;
  const currentKey = useRef(key);
  currentKey.current = key;
  const generation = useRef(0);
  const mounted = useRef(true);
  const [read, setRead] = useState<ReadState | null>(null);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  useEffect(() => {
    mounted.current = true;
    setRead(null);
    setExpandedKey(null);
    return () => { mounted.current = false; generation.current += 1; };
  }, [key]);
  const state = read?.key === key ? read : null;
  const label = reference.kind === 'mail_message' ? 'message' : 'answer';
  const inlineMessage = reference.kind === 'mail_message' && requestQuestion !== undefined;
  const expanded = expandedKey === key;

  async function open() {
    const requestNumber = ++generation.current;
    setRead({ key, loading: true });
    try {
      const value = await getLivePeerRecord(runId, reference);
      if (!mounted.current || currentKey.current !== key || generation.current !== requestNumber) return;
      if (value.runId !== runId || value.reference?.kind !== reference.kind || value.reference.id !== reference.id || value.reference.version !== reference.version) {
        setRead({ key, error: 'The response did not match the selected run and exact record. Content was withheld.' });
      } else if (value.status !== 'available' && value.status !== 'unavailable') {
        setRead({ key, error: 'The response status was not recognized. Content was withheld.' });
      } else if (value.status === 'available' && !Object.hasOwn(value, 'content')) {
        setRead({ key, error: 'The service marked this content available but did not include it. No text is shown.' });
      } else {
        setRead({ key, value });
      }
    } catch (cause) {
      if (!mounted.current || currentKey.current !== key || generation.current !== requestNumber) return;
      const auth = cause instanceof Error && /\b(?:401|403)\b/.test(cause.message);
      setRead({ key, error: auth ? 'Access to this saved content was denied or the local session expired. Reconnect and retry.' : 'Could not read this exact saved content. Check the local connection and retry.' });
    }
  }

  const value = state?.value;
  const name = (id?: string | null) => id ? snapshot.agents.find(agent => agent.id === id)?.name ?? `Agent identity unavailable (${id})` : 'Actor not recorded';
  const showExact = !inlineMessage || expanded;
  return <section className={`peer-record-reader${inlineMessage ? ' peer-record-inline-message' : ''}`} aria-label={`Exact saved ${label} reader`}>
    {inlineMessage && (!expanded || value?.status !== 'available') && <>
      <p className="peer-message-label">Saved request</p>
      <blockquote className="peer-message-quote">{requestQuestion}</blockquote>
      <p className="peer-message-note">This is the saved request. Expand below to read the separate message record.</p>
    </>}
    {inlineMessage ? <button type="button" className="peer-record-read-button" aria-expanded={expanded} onClick={() => {
      setExpandedKey(expanded ? null : key);
      if (!expanded && !state?.value && !state?.loading) void open();
    }}>{expanded ? 'Collapse exact message' : 'Expand exact message'}</button>
      : <button type="button" className="peer-record-read-button" onClick={() => void open()} disabled={state?.loading}>Read exact saved {label} →</button>}
    {showExact && state?.loading && <p role="status">Reading the saved {label}…</p>}
    {showExact && state?.error && <p role="alert">{state.error}</p>}
    {showExact && value?.status === 'unavailable' && <p role="status">{unavailableReason(value.reason)}</p>}
    {inlineMessage && expanded && (state?.error || value?.status === 'unavailable') && <button type="button" className="peer-record-read-button" onClick={() => void open()}>Retry exact message</button>}
    {showExact && value?.status === 'available' && <div className="peer-record-content">
      {inlineMessage && <p className="peer-message-label">Exact saved message</p>}
      <p className="peer-record-direction"><strong>{name(value.senderId)} → {name(value.recipientId)}</strong>{value.createdAt && <> · saved <time dateTime={value.createdAt} title={value.createdAt}>{formatActivityTimestamp(value.createdAt)}</time></>}{reference.kind === 'mail_answer' && value.partial === true ? ' · partial answer' : ''}</p>
      {reference.kind === 'mail_message' && (value.deliveredAt || value.acknowledgedAt) && <p>{value.deliveredAt && <>Delivered <time dateTime={value.deliveredAt} title={value.deliveredAt}>{formatActivityTimestamp(value.deliveredAt)}</time>. </>}{value.acknowledgedAt && <>Acknowledged <time dateTime={value.acknowledgedAt} title={value.acknowledgedAt}>{formatActivityTimestamp(value.acknowledgedAt)}</time>.</>}</p>}
      {value.redacted && <p className="peer-record-redaction">Secret values were redacted from this saved content.</p>}
      <div className="peer-record-body">{reference.kind === 'mail_answer' ? <AnswerContent content={value.content} /> : inlineMessage ? <MessageContent content={value.content} /> : <ReadableValue value={value.content} />}</div>
      <details><summary>Exact record reference</summary><p>{reference.kind.replaceAll('_', ' ')} · {reference.id} · v{reference.version}{value.originRunId ? ` · originating run ${value.originRunId}` : ''}</p>{value.contentReference && <p>Content reference: {value.contentReference.kind} · {value.contentReference.id} · v{value.contentReference.version}</p>}</details>
    </div>}
  </section>;
}
