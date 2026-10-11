/* Give each saved step a concrete action and outcome without inferring missing
   tool arguments, message delivery or scientific acceptance from a status. */
import type { ResearchSnapshot } from './generated/contracts';
import { describeLiveAttempt, toolActionLabel } from './attemptOutcome';

type Attempt = ResearchSnapshot['attempts'][number];
const actions: Record<string, [string, string, string]> = {
  read_source_window: ['Retrieved a passage from a saved source', 'Passage saved', 'The tool retrieved part of a source already saved in this workspace. Inspect the exact passage and next model input to check whether the model received it.'],
  read_context_record: ['Opened a saved task record', 'Record returned', 'The agent opened an earlier record of this task. Inspect this step to see which record and what information was returned.'],
  read_context_page: ['Read a list of saved task records', 'Records returned', 'The agent requested a page of task history. Inspect this step for the records returned and whether more pages remain.'],
  read_source_inventory: ['Listed the saved sources', 'Source list returned', 'The agent checked which sources were already available. No new source was retrieved by this action.'],
  peer_request: ['Requested help from another agent', 'Peer request saved', 'The agent saved a request for another agent. Inspect this step for who was asked and the exact message. Delivery, reply and review are recorded separately.'],
  discover_capabilities: ['Looked up the available tools', 'Tool descriptions returned', 'The agent asked the system which tools were available and received their descriptions. This was a tool-directory lookup, not a search for research evidence.'],
  request_capability: ['Requested access to a tool', 'Access decision returned', 'The system recorded whether the requested tool could be made available. Inspect this step for the tool and decision.'],
  search_scholarly: ['Searched for publications', 'Search result saved', 'The agent searched for publications. Inspect this step for the query, search service and returned results.'],
  inspect_hit: ['Inspected a saved search result', 'Search result inspected', 'The agent opened a previously returned search result. Inspect this step to see which result and what information was available.'],
  fetch_source: ['Retrieved a selected source', 'Source result saved', 'The agent requested a selected source. Inspect this step for its address and returned content.'],
  select_working_sources: ['Selected sources for the next model call', 'Source selection saved', 'The agent selected which saved sources to include in its next model input.'],
  review_peer_answer: ['Reviewed another agent’s reply', 'Review saved', 'The agent recorded its assessment of a peer reply. Inspect this step for the reply, assessment and any unresolved disagreement.'],
  publish_result: ['Submitted a research contribution', 'Submission result saved', 'The agent submitted its contribution. Inspect the returned result to see whether it was accepted.'],
  list_venues: ['Looked up places to search', 'Search locations returned', 'The agent consulted the saved directory of search locations. This action alone does not retrieve publications.'],
  propose_venue: ['Suggested another place to search', 'Suggestion saved', 'The agent proposed a search location for this research.'],
  navigate_venue: ['Opened a search location', 'Navigation result saved', 'The agent opened a search location. Inspect the returned links and access information.'],
};

export function stepPresentation(snapshot: ResearchSnapshot, attempt: Attempt) {
  const outcome = describeLiveAttempt(snapshot, attempt);
  const provider = outcome?.configuredProvider;
  const providerName = provider?.toLowerCase() === 'groq' ? 'Groq' : provider || null;
  const providerText = (value: string) => providerName ? value.replace(/The provider\b/g, providerName).replace(/the provider\b/g, providerName) : value;
  const tool = attempt.toolCalls.length === 1 ? attempt.toolCalls[0] : undefined;
  let title = outcome?.label ?? (tool ? toolActionLabel(tool.name) : 'Saved work step');
  const state = outcome?.badgeStatus ?? attempt.status;
  let outcomeLabel = ({ tool_recovery_needed: 'Saved result needs recovery', tool_waiting: 'Awaiting tool result', metadata_only: 'Reference information only', provider_rate_limit: 'Rate limit', provider_rejection: 'Call rejected', accepted_report: 'Report accepted', response_saved: 'Model response saved' } as Record<string, string>)[state] ?? state.replaceAll('_', ' ');
  let summary = attempt.resultSummary || outcome?.summary || 'No result summary is saved for this step.';
  let actorKind: 'agent' | 'system' | 'provider' = 'agent';
  if (tool && (!outcome || outcome.responseStatus === 'tool_requested')) {
    const succeeded = (['completed', 'request_saved'].includes(tool.status) || (tool.name === 'publish_result' && tool.status === 'published' && outcome?.badgeStatus === 'accepted_report'));
    title = succeeded ? actions[tool.name]?.[0] ?? toolActionLabel(tool.name) : `Requested: ${toolActionLabel(tool.name)}`;
    if (succeeded && (outcome?.badgeStatus === 'tool_step_saved' || !outcome)) {
      outcomeLabel = actions[tool.name]?.[1] ?? 'Tool result saved';
      summary = attempt.resultSummary || actions[tool.name]?.[2] || `${tool.name} returned a saved result.`;
    } else if (tool.status === 'rejected') outcomeLabel = 'Tool request rejected';
  }
  if (outcome?.label === 'Preparation superseded before dispatch') {
    actorKind = 'system';
    title = 'Replaced an unsent model request';
    outcomeLabel = 'Not sent to the model';
    summary = 'The system set aside a prepared model input before sending it. The original preparation remains in history. Its replacement and the reason must be checked in the saved records; this row is not an action chosen by the agent.';
  } else if (outcome?.label === 'Model call paused by operator') {
    actorKind = 'system'; title = 'Paused model calls at the operator’s request'; outcomeLabel = 'Paused';
  } else if (outcome?.label.includes('preparation') && !tool) actorKind = 'system';
  if (outcome?.badgeStatus === 'provider_rate_limit') { actorKind = 'provider'; title = `${providerName ?? 'Model service'} declined the call: rate limit`; }
  if (outcome?.badgeStatus === 'provider_rejection') { actorKind = 'provider'; title = `${providerName ?? 'Model service'} rejected the call`; }
  return { title, outcomeLabel, summary: providerText(summary), toolName: tool?.name ?? null, providerName, actorKind };
}
