/* Render saved calls plainly; new records use the host's exact trace links. */
import { useEffect, useState } from 'react';
import { AgentBadge } from './AgentBadge';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { ProviderReasoningText } from './LiveAgentReasoning';
import type { PassageView, ResearchSnapshot, SourceView } from './generated/contracts';
import { getLiveCalls } from './live';
import './LiveCallInspector.css';

type Json = Record<string, unknown>;
const obj = (value: unknown): Json | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const str = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null;
const savedText = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? 'No content saved';
const count = (value: unknown): number | null => Number.isInteger(value) && Number(value) >= 0 ? Number(value) : null;
const exactRef = (value: unknown): string | null => {
  const record = obj(value);
  const kind = str(record?.kind);
  const id = str(record?.id);
  const version = record?.version;
  return kind && id && Number.isInteger(version) && Number(version) > 0 ? `${kind} · ${id} · version ${version}` : null;
};

function readableArguments(value: unknown): string {
  if (typeof value !== 'string') return savedText(value);
  try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
}

function sameToolIdentity(requested: unknown, returned: unknown): boolean {
  const request = obj(requested);
  const result = obj(returned);
  const id = str(request?.id);
  const name = str(obj(request?.function)?.name);
  return Boolean(id && name && id === str(result?.tool_call_id) && name === str(result?.name));
}

function savedToolOutcome(value: unknown) {
  const record = obj(value);
  const hasContent = record !== null && Object.prototype.hasOwnProperty.call(record, 'content');
  const raw = record?.content;
  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try { parsed = JSON.parse(raw); } catch { /* A plain-text result has no structured status. */ }
  }
  const content = obj(parsed);
  return {
    status: str(content?.status),
    reason: str(content?.reason),
    nextAction: str(content?.next_action),
    hasContent,
    content,
    exactText: raw === '' ? '"" (empty saved string)' : savedText(raw),
  };
}

function knownReturn(name: string, content: Json | null): boolean {
  if (!content) return false;
  if (name === 'read_source_window') return count(content.returned_chars) !== null && Array.isArray(content.passages);
  if (name === 'read_source_inventory') return content.schema === 'ora.source_inventory_page.v1' && Array.isArray(content.items);
  if (name === 'read_context_page') return content.schema === 'ora.context_omission_page.v2' && Array.isArray(content.items);
  if (name === 'read_context_record') return content.schema === 'ora.context_record_window.v1' && typeof content.text === 'string';
  if (name === 'discover_capabilities') return obj(content.granted) !== null && Array.isArray(content.exposed);
  return false;
}

function returnMatchesRequest(name: string, args: Json | null, content: Json | null): boolean {
  if (name !== 'read_source_window') return true;
  const copy = obj(content?.copy_ref);
  return Boolean(args && copy?.kind === 'source_copy' && str(args.source_id)
    && copy.id === args.source_id && Number.isInteger(args.source_version)
    && copy.version === args.source_version);
}

function savedRange(passage: PassageView) {
  const match = /^(.*?)(?:;\s*)?chars=(\d+):(\d+)\s*$/.exec(passage.locator);
  if (!match) return null;
  const start = Number(match[2]);
  const end = Number(match[3]);
  const characters = Array.from(passage.text);
  return Number.isSafeInteger(start) && Number.isSafeInteger(end) && end > start && end - start === characters.length
    ? { base: match[1].trim(), start, end, characters } : null;
}

function excerptRelationships(source: SourceView, index: number): string[] {
  const current = savedRange(source.passages[index]);
  if (!current || source.passages.length === 1) return [];
  const overlaps = source.passages.flatMap((passage, otherIndex) => {
    if (otherIndex === index) return [];
    const other = savedRange(passage);
    if (!other || other.base !== current.base) return [];
    const start = Math.max(current.start, other.start);
    const end = Math.min(current.end, other.end);
    if (end <= start) return [];
    const matches = current.characters.slice(start - current.start, end - current.start).join('')
      === other.characters.slice(start - other.start, end - other.start).join('');
    return [{ other, otherIndex, overlap: end - start, matches }];
  });
  const matching = overlaps.filter(item => item.matches).sort((a, b) =>
    Number(b.otherIndex < index) - Number(a.otherIndex < index)
    || b.overlap - a.overlap || a.otherIndex - b.otherIndex);
  const closest = matching[0];
  const relationships: string[] = [];
  if (closest) {
    const name = `Evidence excerpt ${closest.otherIndex + 1}`;
    if (current.start === closest.other.start && current.end === closest.other.end) relationships.push(`Same saved range and text as ${name}.`);
    else if (current.start <= closest.other.start && current.end >= closest.other.end) relationships.push(`Contains ${name} and adds ${(current.end - current.start - closest.overlap).toLocaleString()} characters.`);
    else if (closest.other.start <= current.start && closest.other.end >= current.end) relationships.push(`A shorter selection within ${name}; no additional text in that range.`);
    else relationships.push(`Overlaps ${name} by ${closest.overlap.toLocaleString()} matching characters.`);
  }
  const conflicts = overlaps.filter(item => !item.matches).sort((a, b) => a.otherIndex - b.otherIndex);
  if (conflicts.length) relationships.push(`Different wording at an overlapping saved location: ${conflicts.map(item => `Evidence excerpt ${item.otherIndex + 1}`).join(', ')}. Treat these as separate excerpts.`);
  if (!relationships.length) relationships.push('No overlapping saved character range was confirmed with other excerpts in this source.');
  return relationships;
}

function evidenceExcerpt(snapshot: ResearchSnapshot, content: Json, passage: Json | null) {
  const copy = obj(content.copy_ref);
  const spanId = str(passage?.span_id);
  const locator = str(passage?.locator);
  const passageRef = obj(passage?.passage_ref);
  if (!copy || !spanId || !locator || typeof passage?.text_preview !== 'string'
    || passageRef?.kind !== 'source_passage' || !Number.isInteger(passageRef.version)
    || Number(passageRef.version) <= 0 || passageRef.id !== `${copy.id}:${spanId}`
    || !Array.isArray(content.passage_refs)
    || content.passage_refs.filter(value => {
      const saved = obj(value);
      return saved?.kind === passageRef.kind && saved?.id === passageRef.id && saved?.version === passageRef.version;
    }).length !== 1) return null;
  const sources = snapshot.sources.filter(source => source.id === copy.id && source.copyId === copy.id && source.version === copy.version
    && !(str(content.content_sha256) && str(source.contentHash) && content.content_sha256 !== source.contentHash));
  if (sources.length !== 1) return null;
  const source = sources[0];
  const matches = source.passages.map((item, index) => ({ item, index })).filter(({ item }) => item.spanId === spanId
    && item.locator === locator && item.text === passage.text_preview);
  return matches.length === 1 ? { number: matches[0].index + 1, relationships: excerptRelationships(source, matches[0].index) } : null;
}

function SourceWindowRequest({ args }: { args: Json }) {
  return <div className="live-call-source-request">
    <dl>
      <div><dt>Saved source copy version</dt><dd>{count(args.source_version) ?? 'Not recorded'}</dd></div>
      <div><dt>Section offset</dt><dd>{count(args.block_offset) ?? 'Not recorded'}</dd></div>
      <div><dt>Start at character</dt><dd>{count(args.char_offset) ?? 'Not recorded'}</dd></div>
      <div><dt>Requested maximum passage length</dt><dd>{count(args.char_limit) !== null ? `${count(args.char_limit)} characters` : 'Not recorded'}</dd></div>
    </dl>
    <p>Offsets start at 0 in the saved text; a section offset is a position, not a verified section heading.</p>
  </div>;
}

function ReadableHostReturn({ name, content, snapshot }: { name: string; content: Json; snapshot: ResearchSnapshot }) {
  if (name === 'read_source_window') {
    const passages = content.passages as unknown[];
    const cursor = obj(content.next_cursor);
    const remaining = count(content.remaining_in_block_chars);
    return <div className="live-call-readable-result">
      <p><strong>Excerpt returned:</strong> {count(content.returned_chars)} characters of saved text{count(content.effective_char_limit) !== null ? ` · ${count(content.effective_char_limit)}-character effective limit` : ''}{remaining !== null ? ` · ${remaining} characters remaining in this block` : ''}.</p>
      {passages.length ? passages.map((raw, index) => {
        const passage = obj(raw);
        const match = evidenceExcerpt(snapshot, content, passage);
        return <section key={index}>
          <strong>{match ? `Evidence excerpt ${match.number}` : `Returned passage ${index + 1} · Evidence number unavailable`}{str(passage?.locator) ? ` · ${str(passage?.locator)}` : ''}</strong>
          {match?.relationships.map((relationship, relationIndex) => <p className="live-call-excerpt-relation" key={relationIndex}>{relationship}</p>)}
          {!match && <p className="live-call-excerpt-relation">This returned passage does not match an exact numbered excerpt in the selected snapshot.</p>}
          {typeof passage?.text_preview === 'string' ? <blockquote>{passage.text_preview}</blockquote> : <p>No excerpt text was saved for this passage.</p>}
        </section>;
      }) : <p>No passage was returned in this window.</p>}
      {cursor && <p>Next saved reading position: section offset {count(cursor.block_offset) ?? 'not recorded'}, character {count(cursor.char_offset) ?? 'not recorded'}.</p>}
      {content.next_cursor === null && <p>No next reading position was saved.</p>}
    </div>;
  }
  if (name === 'read_source_inventory') {
    const items = content.items as unknown[];
    return <div className="live-call-readable-result"><p><strong>Saved source inventory:</strong> {items.length} on this page{count(content.total) !== null ? ` · ${count(content.total)} total` : ''}.</p>
      {items.map((value, index) => { const item = obj(value); return <p key={index}>{index + 1}. {str(item?.title) ?? 'Title not saved'}{exactRef(item?.ref) ? ` · ${exactRef(item?.ref)}` : ''}{str(item?.model_input_status) ? ` · ${str(item?.model_input_status)?.replaceAll('_', ' ')}` : ''}</p>; })}
      {count(content.next_cursor) !== null && <p>Next saved page cursor: {count(content.next_cursor)}.</p>}
    </div>;
  }
  if (name === 'read_context_page') {
    const items = content.items as unknown[];
    return <div className="live-call-readable-result"><p><strong>Saved task-record page:</strong> {items.length} on this page{count(content.total) !== null ? ` · ${count(content.total)} total` : ''}.</p>
      {items.map((value, index) => { const item = obj(value); const summary = obj(item?.summary); return <p key={index}>{index + 1}. {str(item?.type)?.replaceAll('_', ' ') ?? 'Saved record'}{exactRef(item?.ref) ? ` · ${exactRef(item?.ref)}` : ''}{str(summary?.kind) ? ` · ${str(summary?.kind)?.replaceAll('_', ' ')}` : ''}</p>; })}
      {count(content.next_cursor) !== null && <p>Next saved page cursor: {count(content.next_cursor)}.</p>}
    </div>;
  }
  if (name === 'read_context_record') return <div className="live-call-readable-result"><p><strong>Saved record window:</strong> {exactRef(content.ref) ?? 'reference not recorded'} · {count(content.returned_chars) ?? 'unknown number of'} characters returned.</p><pre>{content.text as string}</pre>{count(content.next_offset) !== null && <p>Next saved text offset: {count(content.next_offset)}.</p>}</div>;
  const granted = obj(content.granted) ?? {};
  const exposed = (content.exposed as unknown[]).filter((item): item is string => typeof item === 'string');
  return <div className="live-call-readable-result"><p><strong>Available capabilities:</strong> {Object.keys(granted).length} granted · {exposed.length} exposed in this call.</p>
    {Object.entries(granted).map(([toolId, purpose]) => <p key={toolId}>{toolId}{typeof purpose === 'string' ? ` · ${purpose}` : ''}{exposed.includes(toolId) ? ' · exposed' : ''}</p>)}
  </div>;
}

const TOOL_VERBS: Record<string, [string, string]> = {
  discover_capabilities: ['Checked available tools', 'Check available tools'],
  request_capability: ['Requested tool access', 'Request tool access'],
  search_scholarly: ['Searched scholarly sources', 'Search scholarly sources'],
  inspect_hit: ['Inspected a saved search result', 'Inspect a saved search result'],
  fetch_source: ['Fetched a selected source', 'Fetch a selected source'],
  read_source_window: ['Read a saved source excerpt', 'Read a saved source excerpt'],
  read_source_inventory: ['Listed saved sources', 'List saved sources'],
  read_context_record: ['Opened a saved task record', 'Open a saved task record'],
  read_context_page: ['Listed saved task records', 'List saved task records'],
  peer_request: ['Saved a peer request', 'Request peer help'],
  navigate_venue: ['Opened a suggested search location', 'Open a suggested search location'],
  publish_result: ['Submitted a research contribution', 'Submit a research contribution'],
};

function toolAction(name: string, outcome: ReturnType<typeof savedToolOutcome> | null, readable: boolean): string {
  const verbs = TOOL_VERBS[name];
  if (!verbs) return outcome ? `Host returned from ${name}` : `Requested ${name}`;
  if (!outcome) return `Requested: ${verbs[1]}`;
  if (outcome.status && ['rejected', 'failed', 'error', 'unavailable'].includes(outcome.status)) return `Tried to ${verbs[1].toLowerCase()}`;
  if (!outcome.status) return readable ? verbs[0] : `Host returned from: ${verbs[1]}`;
  return verbs[0];
}

function parsedArguments(request: Json | null): Json | null {
  const raw = obj(request?.function)?.arguments;
  if (typeof raw !== 'string') return obj(raw);
  try { return obj(JSON.parse(raw)); } catch { return null; }
}

function toolTarget(name: string, args: Json | null, snapshot: ResearchSnapshot, authorId: string | null): string | null {
  if (!args) return null;
  if (name === 'read_source_window') {
    const sourceId = str(args.source_id);
    const version = args.source_version;
    if (!sourceId || !Number.isInteger(version) || Number(version) <= 0) return null;
    const sources = snapshot.sources.filter(source => source.id === sourceId && source.version === version);
    return sources.length === 1 ? `Saved source: ${sources[0].title}` : `Saved source copy: ${sourceId} (title not verified here)`;
  }
  if (name === 'read_context_record') {
    const kind = str(args.kind);
    const id = str(args.id);
    const version = args.version;
    return kind && id && Number.isInteger(version) && Number(version) > 0
      ? `Record: ${kind} · ${id} · version ${version}` : null;
  }
  if (name === 'peer_request') {
    const peerId = str(args.target_instance_id);
    const sender = snapshot.agents.filter(agent => agent.id === authorId);
    const recipient = snapshot.agents.filter(agent => agent.id === peerId);
    return sender.length === 1 && recipient.length === 1 && sender[0].id !== recipient[0].id
      ? `Requested peer direction: ${sender[0].name} → ${recipient[0].name}` : peerId ? `Requested peer: ${peerId} (identity not verified here)` : null;
  }
  if (name === 'inspect_hit' || name === 'fetch_source') return str(args.hit_id) ? `Saved search result: ${str(args.hit_id)}` : null;
  if (name === 'search_scholarly') return str(args.route) ? `Search route: ${str(args.route)}` : null;
  if (name === 'navigate_venue') return str(args.venue_id) ? `Saved search location: ${str(args.venue_id)}` : null;
  return null;
}

function linkedPeers(call: Json, snapshot: ResearchSnapshot) {
  const requester = str(call.agentId);
  if (!requester || !Array.isArray(call.messages)) return [];
  const links: Array<{ requestId: string; peerId: string; requesterId: string; peerName: string | null; requesterName: string | null; reply: string; attemptId: string | null }> = [];
  for (const value of call.messages) {
    const message = obj(value);
    if (message?.role !== 'user' || typeof message.content !== 'string') continue;
    let envelope: Json | null = null;
    try { envelope = obj(JSON.parse(message.content)); } catch { /* Ordinary text is not linkage. */ }
    const id = str(envelope?.peerRequestId);
    const reply = str(envelope?.peerResponse);
    if (!id || !reply || links.some(link => link.requestId === id)) continue;
    const requests = snapshot.messages.filter(item => item.requestId === id && item.fromAgentId === requester && item.toAgentId !== requester && item.status === 'fulfilled');
    if (requests.length !== 1) continue;
    const request = requests[0];
    const delivered = snapshot.messages.filter(item => item.requestId === id && item.status === 'delivered' && item.fromAgentId === request.toAgentId && item.toAgentId === requester && item.taskId === request.taskId && item.summary === reply);
    if (delivered.length !== 1) continue;
    const callTime = Date.parse(str(call.createdAt) ?? '');
    const attempts = snapshot.attempts.filter(item => {
      const ended = Date.parse(item.endedAt ?? '');
      return item.taskId === request.taskId && item.agentId === request.toAgentId
        && item.resultSummary === reply && Number.isFinite(ended)
        && Number.isFinite(callTime) && ended <= callTime;
    });
    links.push({ requestId: id, reply, peerId: request.toAgentId, requesterId: requester,
      peerName: snapshot.agents.find(item => item.id === request.toAgentId)?.name ?? null,
      requesterName: snapshot.agents.find(item => item.id === requester)?.name ?? null,
      attemptId: attempts.length === 1 ? attempts[0].id : null });
  }
  return links;
}

export function LiveCallInspector({ runId, attemptId, snapshotId, snapshot, onOpenAttempt }: {
  runId: string | null; attemptId: string; snapshotId: string; snapshot: ResearchSnapshot;
  onOpenAttempt?: (attemptId: string) => void;
}) {
  const identity = `${runId ?? ''}:${attemptId}`;
  const [loaded, setLoaded] = useState<{ identity: string; snapshotId: string; records: unknown[] } | null>(null);
  const [failure, setFailure] = useState<{ identity: string; snapshotId: string; message: string } | null>(null);
  const records = loaded?.identity === identity ? loaded.records : null;
  const error = failure?.identity === identity && failure.snapshotId === snapshotId ? failure.message : null;
  const refreshing = records !== null && loaded?.snapshotId !== snapshotId && !error;
  useEffect(() => {
    if (!runId) return;
    let active = true;
    setFailure(null);
    getLiveCalls(runId, attemptId)
      .then(result => { if (active) setLoaded({ identity, snapshotId, records: result.records }); })
      .catch(cause => { if (active) setFailure({ identity, snapshotId, message: cause instanceof Error ? cause.message : 'Call records unavailable.' }); });
    return () => { active = false; };
  }, [runId, attemptId, snapshotId, identity]);
  return <section className="live-call-inspector" aria-label="Actual model call records">
    <h4>Model calls</h4>
    {refreshing && <p role="status">Checking exact saved calls for the newer snapshot. The call details below are from the prior snapshot until this check completes.</p>}
    {error && <p role="alert">{records === null ? error : `Could not refresh exact calls for this snapshot. Prior saved call details remain below. ${error}`}</p>}
    {records === null ? !error && <p role="status">Loading call records…</p> : records.length === 0 ? <p>No call record has been saved for this attempt.</p> : records.map((raw, index) => {
      const call = obj(raw);
      const response = obj(call?.response);
      const trace = obj(call?.trace);
      const canonical = trace?.basis === 'linked_saved_records';
      const traceAuthor = canonical ? obj(trace.author) : null;
      const authorId = str(traceAuthor?.agentId) ?? str(call?.agentId);
      const authorName = canonical ? str(traceAuthor?.name)
        : snapshot.agents.find(item => item.id === authorId)?.name ?? null;
      const outputs = canonical && Array.isArray(trace.outputs) ? trace.outputs.map(obj).filter(item => item !== null) : null;
      const contentPath = outputs?.find(item => item.recordPath === 'response.content' && item.authoredByAgentId === authorId);
      const toolsPath = outputs?.find(item => item.recordPath === 'response.tool_calls' && item.authoredByAgentId === authorId);
      const content = !canonical || contentPath ? str(response?.content) : null;
      const results = Array.isArray(call?.toolResults) ? call.toolResults : [];
      const validation = obj(call?.providerValidation);
      const requested = (!canonical || toolsPath) && Array.isArray(response?.tool_calls) ? response.tool_calls : [];
      const linkedResultIndexes = new Set<number>();
      requested.forEach(request => {
        const matches = results.map((result, resultIndex) => ({ result, resultIndex })).filter(({ result }) => sameToolIdentity(request, result));
        if (matches.length === 1 && requested.filter(candidate => sameToolIdentity(candidate, matches[0].result)).length === 1) {
          linkedResultIndexes.add(matches[0].resultIndex);
        }
      });
      const tracedPeers = canonical && Array.isArray(trace.peerInputs) ? trace.peerInputs.map(obj).filter(item => item !== null) : [];
      const retryIndex = str(call?.id) ? records.findIndex(item => obj(item)?.retryOfCallId === call?.id) : -1;
      return <article className="panel live-call-card" key={`${str(call?.runId) ?? runId}:${str(call?.id) ?? index}`}>
        <h5>Call {index + 1} · <AgentBadge name={authorName} agentId={authorId} /></h5>
        <p className="live-call-scan"><strong>{str(call?.status)?.replaceAll('_', ' ') ?? 'Status not recorded'}</strong> · {str(call?.createdAt) ? <time dateTime={str(call?.createdAt)!}>{formatActivityTimestamp(str(call?.createdAt)!)}</time> : 'Time not recorded'}</p>
        {validation?.kind === 'missing_required_argument' && str(validation.function_name) && str(validation.field) && <p>Provider tool validation: {str(validation.function_name)} lacked required {str(validation.field)}. The provider rejected this call before the host tool ran.</p>}
        {call?.status === 'rate_limited' ? <p>Quota wait: no model response was returned for this call.</p> : content ? <div><h6>Model response</h6><pre>{content}</pre></div> : <p>No final text response was linked for this call.</p>}
        {requested.length > 0 && <div><h6>Exact tool actions</h6><p>Host returns show tool outcomes; report acceptance is recorded separately.</p>{requested.map((item, toolIndex) => {
          const request = obj(item);
          const name = str(obj(request?.function)?.name) ?? 'Unnamed function';
          const matches = results.map((result, index) => ({ result, index })).filter(({ result }) => sameToolIdentity(item, result));
          const duplicateRequests = requested.filter(candidate => sameToolIdentity(candidate, { tool_call_id: request?.id, name })).length > 1;
          const completeIdentity = str(request?.id) && str(obj(request?.function)?.name);
          const matched = completeIdentity && !duplicateRequests && matches.length === 1 ? matches[0] : null;
          const outcome = matched ? savedToolOutcome(matched.result) : null;
          const args = parsedArguments(request);
          const readable = Boolean(outcome && knownReturn(name, outcome.content) && returnMatchesRequest(name, args, outcome.content));
          const target = completeIdentity && !duplicateRequests ? toolTarget(name, args, snapshot, authorId) : null;
          return <div key={`${str(request?.id) ?? 'unknown'}:${toolIndex}`} className="live-call-tool"><strong>{toolAction(name, outcome, readable)}</strong>
            {target && <p>{target}</p>}
            {!completeIdentity ? <p>Request identity is incomplete; no saved host result can be linked.</p>
              : duplicateRequests || matches.length > 1 ? <p>Saved request or result identity is ambiguous; no unique host result can be linked.</p>
                : !matched ? <p>No saved host result is linked yet. This does not establish whether the tool ran.</p> : null}
            {name === 'read_source_window' && args && str(args.source_id) && Number.isInteger(args.source_version) && Number(args.source_version) > 0
              ? <><SourceWindowRequest args={args} /><details className="inline-detail"><summary>Exact source-read arguments</summary><pre>{readableArguments(obj(request?.function)?.arguments)}</pre></details></>
              : <div className="live-call-payload"><strong>Arguments sent</strong><pre>{readableArguments(obj(request?.function)?.arguments ?? 'Arguments not recorded')}</pre></div>}
            {matched && <>
              {outcome?.status && <p><strong>{outcome.status === 'rejected' ? 'Tool rejected' : `Tool outcome: ${outcome.status.replaceAll('_', ' ')}`}</strong></p>}
              {outcome?.reason && <p><strong>Reason:</strong> {outcome.reason}</p>}
              {outcome?.nextAction && <p><strong>Saved next action:</strong> {outcome.nextAction}</p>}
              {!outcome?.status && <p>{outcome?.hasContent ? 'A tool return was saved; no separate outcome status was recorded here.' : 'No tool return content field was saved for this record.'}</p>}
              {outcome?.hasContent && (readable && !['rejected', 'failed', 'error', 'unavailable'].includes(outcome.status ?? '')
                ? <div className="live-call-payload"><ReadableHostReturn name={name} content={outcome.content!} snapshot={snapshot} /><details className="inline-detail"><summary>Exact saved host return JSON</summary><pre>{outcome.exactText}</pre></details></div>
                : <div className="live-call-payload"><strong>{outcome.content ? 'Host return (unclassified format)' : 'Host return'}</strong><pre>{outcome.exactText}</pre></div>)}
            </>}
            <details className="inline-detail"><summary>Exact tool identity</summary><p>Function: {name} · call ID: {str(request?.id) ?? 'not recorded'}{matched ? ` · host result ${matched.index + 1}` : ''}</p></details>
          </div>;
        })}</div>}
        {results.some((_, resultIndex) => !linkedResultIndexes.has(resultIndex)) && <div><h6>Unlinked saved host returns</h6>{results.map((item, toolIndex) => {
          if (linkedResultIndexes.has(toolIndex)) return null;
          const result = obj(item);
          const outcome = savedToolOutcome(item);
          const matchingRequests = requested.filter(requestedItem => sameToolIdentity(requestedItem, item));
          const matchingResults = results.filter(candidate => matchingRequests.length === 1 && sameToolIdentity(matchingRequests[0], candidate));
          return <div key={toolIndex} className="live-call-tool"><strong>Unlinked host return {toolIndex + 1}</strong>
            {str(result?.tool_call_id) && str(result?.name) ? <p>{matchingRequests.length > 1 || matchingResults.length > 1 ? 'Saved request or result identity is ambiguous; no unique requested function can be linked.' : 'No matching requested function is linked by both saved call ID and name.'}</p>
              : <p>Saved result identity is incomplete; no requested function can be linked.</p>}
            {outcome.status && <p><strong>{outcome.status === 'rejected' ? 'Tool rejected' : `Tool outcome: ${outcome.status.replaceAll('_', ' ')}`}</strong></p>}
            {outcome.reason && <p><strong>Reason:</strong> {outcome.reason}</p>}
            {outcome.nextAction && <p><strong>Saved next action:</strong> {outcome.nextAction}</p>}
            {!outcome.status && <p>{outcome.hasContent ? 'A tool return was saved; no separate outcome status was recorded here.' : 'No tool return content field was saved for this record.'}</p>}
            {outcome.hasContent && <div className="live-call-payload"><strong>Host return</strong><pre>{outcome.exactText}</pre></div>}
            <details className="inline-detail"><summary>Exact result identity</summary><p>Function: {str(result?.name) ?? 'not recorded'} · call ID: {str(result?.tool_call_id) ?? 'not recorded'}</p></details>
          </div>;
        })}</div>}
        {canonical ? tracedPeers.map((link, linkIndex) => link.linkStatus === 'linked' ? <div className="live-peer-link" key={str(link.peerRequestId) ?? linkIndex}>
          <h6><AgentBadge name={str(link.peerAgentName)} agentId={str(link.peerAgentId)} /> reply used by <AgentBadge name={authorName} agentId={authorId} /></h6>
          <p>Saved peer request {str(link.peerRequestId) ?? 'not recorded'} · input {str(link.inputRecordPath) ?? 'not recorded'}</p>
          <p>Request record {str(link.requestMessageId) ?? 'not recorded'} · reply record {str(link.responseMessageId) ?? 'not recorded'}</p>
          <blockquote>{str(link.peerResponse) ?? 'No reply text recorded.'}</blockquote>
          {link.sourceAttemptResolution === 'unique' && str(link.sourceAttemptId) && onOpenAttempt ? <button type="button" className="inline-link" onClick={() => onOpenAttempt(str(link.sourceAttemptId)!)}>Open {str(link.peerAgentName) ?? 'peer'} attempt</button> : <p>Peer attempt: {str(link.sourceAttemptResolution) ?? 'unknown'}.</p>}
        </div> : <p key={str(link.peerRequestId) ?? linkIndex}>Peer input {str(link.peerRequestId) ?? 'unknown'}: saved relationship {str(link.linkStatus) ?? 'unknown'}; no author or attempt is inferred.</p>)
          : call && linkedPeers(call, snapshot).map(link => <div className="live-peer-link" key={link.requestId}><h6><AgentBadge name={link.peerName} agentId={link.peerId} /> reply used by <AgentBadge name={link.requesterName} agentId={link.requesterId} /></h6><p>Saved peer request {link.requestId}</p><blockquote>{link.reply}</blockquote>{link.attemptId && onOpenAttempt ? <button type="button" className="inline-link" onClick={() => onOpenAttempt(link.attemptId!)}>Open {link.peerName ?? 'peer'} attempt</button> : !link.attemptId ? <p>Peer attempt cannot be identified uniquely from these saved records.</p> : null}</div>)}
        {retryIndex >= 0 && <p>Retried in Call {retryIndex + 1}; inspect that call for its separate outcome.</p>}
        {str(call?.retryOfCallId) && <p>This call retries saved call {str(call?.retryOfCallId)}.</p>}
        {str(call?.id) && str(call?.runId) && str(call?.agentId) && call?.attemptId === attemptId
          ? <div><h6>Provider-returned reasoning</h6><p>{call?.status === 'prepared' || call?.status === 'superseded'
            ? `No model response was returned for this ${call.status} call.`
            : ['restricted', 'returned', 'truncated'].includes(str(obj(call.returnedReasoning)?.availability) ?? '')
              ? 'This call indicates saved provider text. Read it only when needed.'
              : call?.status === 'rate_limited' ? 'The provider rejected this request; no reasoning text was returned.'
                : 'No saved provider reasoning text is indicated for this call.'}</p><ProviderReasoningText call={{ id: str(call.id)!, runId: str(call.runId)!, attemptId, agentId: str(call.agentId)!, status: str(call.status) ?? 'unknown', availability: str(obj(call.returnedReasoning)?.availability) ?? 'unknown' }} /></div>
          : <p>The exact call identity is incomplete; saved provider reasoning cannot be read from this record.</p>}
        <details className="inline-detail technical-disclosure">
          <summary>Exact call metadata and raw record</summary>
          <dl className="detail-list">
            <div><dt>Model</dt><dd>{str(response?.model) ?? 'No model response recorded'}</dd></div>
            <div><dt>Phase</dt><dd>{str(call?.phase) ?? 'Not recorded'}</dd></div>
            <div><dt>Started</dt><dd>{str(call?.createdAt) ?? 'Not recorded'}</dd></div>
            <div><dt>Finished</dt><dd>{str(call?.completedAt) ?? 'Not recorded'}</dd></div>
            <div><dt>Originating run</dt><dd>{str(traceAuthor?.runId) ?? str(call?.runId) ?? 'Not recorded'}</dd></div>
            {canonical && <>
              <div><dt>Author record</dt><dd>{str(traceAuthor?.agentId) ?? 'Unknown'} · {str(traceAuthor?.role) ?? 'role not recorded'}</dd></div>
              <div><dt>Task / attempt</dt><dd>{str(traceAuthor?.taskId) ?? 'Unknown'} / {str(traceAuthor?.attemptId) ?? 'Unknown'}</dd></div>
            </>}
            {contentPath && <div><dt>Response path</dt><dd>{str(contentPath.recordPath)}</dd></div>}
            {toolsPath && <div><dt>Function request path</dt><dd>{str(toolsPath.recordPath)}</dd></div>}
          </dl>
          <details className="inline-detail"><summary>Full raw call JSON</summary><pre>{JSON.stringify(raw, null, 2)}</pre></details>
        </details>
      </article>;
    })}
  </section>;
}
