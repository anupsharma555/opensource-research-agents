/* Resolve only saved report pointers and exact source identifiers for display.
   Text matches and peer exchanges remain qualified; they never create citations,
   authorship, or scientific support in the underlying research snapshot. */
import type { ResearchSnapshot } from './generated/contracts';

type Report = ResearchSnapshot['artifacts'][number];
type Source = ResearchSnapshot['sources'][number];
type Attempt = ResearchSnapshot['attempts'][number];
type Message = ResearchSnapshot['messages'][number];

export type ReportReference = {
  sourceId: string;
  spanId: string;
  basis: 'recorded' | 'saved_text';
  source?: Source;
  passage?: Source['passages'][number];
};

const key = (sourceId: string, spanId: string) => JSON.stringify([sourceId, spanId]);

function exactPassage(sources: Source[], sourceId: string, spanId: string) {
  const matchingSources = sources.filter(source => source.id === sourceId);
  const passages = matchingSources.flatMap(source => source.passages.filter(passage => passage.spanId === spanId).map(passage => ({ source, passage })));
  return matchingSources.length === 1 && passages.length === 1 ? passages[0] : undefined;
}

function hasExactPair(body: string, sourceId: string, spanId: string) {
  const token = `${sourceId}::${spanId}`;
  for (let index = body.indexOf(token); index >= 0; index = body.indexOf(token, index + token.length)) {
    const before = body[index - 1];
    const after = body[index + token.length];
    const next = body[index + token.length + 1];
    const validStart = !before || !/[\w.:-]/.test(before);
    const validEnd = !after || (!/[\w:-]/.test(after) && (after !== '.' || !next || !/[\w.-]/.test(next)));
    if (validStart && validEnd) return true;
  }
  return false;
}

export function reportReferences(report: Report, sources: Source[]): ReportReference[] {
  const references: ReportReference[] = [];
  const seen = new Set<string>();
  for (const citation of report.citations) {
    const identity = key(citation.sourceId, citation.spanId);
    if (seen.has(identity)) continue;
    seen.add(identity);
    const matchingSources = sources.filter(source => source.id === citation.sourceId);
    references.push({ sourceId: citation.sourceId, spanId: citation.spanId, basis: 'recorded', source: matchingSources.length === 1 ? matchingSources[0] : undefined, passage: exactPassage(sources, citation.sourceId, citation.spanId)?.passage });
  }
  const candidates = sources.flatMap(source => source.passages.map(passage => ({ source, passage })));
  for (const { source, passage } of candidates) {
    if (!hasExactPair(report.body, source.id, passage.spanId) || !exactPassage(sources, source.id, passage.spanId)) continue;
    const identity = key(source.id, passage.spanId);
    if (seen.has(identity)) continue;
    seen.add(identity);
    references.push({ sourceId: source.id, spanId: passage.spanId, basis: 'saved_text', source, passage });
  }
  return references;
}

export type ProducerResolution = { kind: 'recorded' | 'legacy_match'; attempt: Attempt } | { kind: 'unresolved' | 'ambiguous' | 'absent'; attempt?: never };

export function reportProducer(report: Report, snapshot?: ResearchSnapshot): ProducerResolution {
  if (!snapshot) return { kind: 'absent' };
  if (report.producerAttemptId) {
    const matches = snapshot.attempts.filter(attempt => attempt.id === report.producerAttemptId);
    return matches.length === 1 ? { kind: 'recorded', attempt: matches[0] } : { kind: 'unresolved' };
  }
  const matches = snapshot.attempts.filter(attempt => attempt.status === 'completed' && attempt.resultSummary === report.body);
  return matches.length === 1 ? { kind: 'legacy_match', attempt: matches[0] }
    : { kind: matches.length > 1 ? 'ambiguous' : 'absent' };
}

export type PeerExchange = { request: Message; reply: Message; attempt: Attempt };

export function linkedPeerExchanges(snapshot: ResearchSnapshot, producer: Attempt): PeerExchange[] {
  const groups = new Map<string, Message[]>();
  for (const message of snapshot.messages) {
    const requestId = message.requestId || message.id;
    groups.set(requestId, [...(groups.get(requestId) ?? []), message]);
  }
  const exchanges: PeerExchange[] = [];
  for (const history of groups.values()) {
    history.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    const request = history[0];
    const task = snapshot.tasks.find(item => item.id === request.taskId);
    if (request.fromAgentId !== producer.agentId || request.fromAgentId === request.toAgentId || (task?.id !== producer.taskId && task?.parentTaskId !== producer.taskId)) continue;
    if (!history.some(message => message.status === 'fulfilled' && message.fromAgentId === request.fromAgentId && message.toAgentId === request.toAgentId && message.taskId === request.taskId)) continue;
    const replies = history.filter(message => message.id !== request.id && message.status === 'delivered' && message.fromAgentId === request.toAgentId && message.toAgentId === request.fromAgentId && message.taskId === request.taskId);
    if (replies.length !== 1) continue;
    const reply = replies[0];
    if (!producer.endedAt || !Number.isFinite(Date.parse(producer.endedAt)) || Date.parse(reply.createdAt) > Date.parse(producer.endedAt)) continue;
    const matchingAttempts = snapshot.attempts.filter(attempt => attempt.taskId === request.taskId && attempt.agentId === reply.fromAgentId && attempt.status === 'completed' && attempt.resultSummary === reply.summary && attempt.endedAt && Number.isFinite(Date.parse(attempt.endedAt)) && Date.parse(attempt.endedAt) <= Date.parse(reply.createdAt));
    if (matchingAttempts.length === 1) exchanges.push({ request, reply, attempt: matchingAttempts[0] });
  }
  return exchanges;
}
