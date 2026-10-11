/* Verify readable call provenance from saved IDs and exact peer replies. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ResearchSnapshot } from './generated/contracts';
import { LiveCallInspector } from './LiveCallInspector';
import { getLiveCalls, getLiveReasoning } from './live';
import sample from '../public/sample-run.json';

vi.mock('./live', () => ({ getLiveCalls: vi.fn(), getLiveReasoning: vi.fn() }));

const saved = { ...sample,
  agents: [
    { id: 'worker-a', name: 'Sage', role: 'synthesis', status: 'available' },
    { id: 'worker-b', name: 'Mira', role: 'critique', status: 'available' },
  ],
  attempts: [{ ...sample.attempts[0], id: 'attempt-peer', agentId: 'worker-b', taskId: 'peer-task', resultSummary: 'Exact peer assessment', endedAt: '2026-10-08T11:00:00Z' }],
  messages: [
    { ...sample.messages[0], id: 'request-message', requestId: 'req-1', fromAgentId: 'worker-a', toAgentId: 'worker-b', taskId: 'peer-task', status: 'fulfilled', summary: 'Check the answer.' },
    { ...sample.messages[0], id: 'reply-message', requestId: 'req-1', fromAgentId: 'worker-b', toAgentId: 'worker-a', taskId: 'peer-task', status: 'delivered', summary: 'Exact peer assessment' },
  ],
} as ResearchSnapshot;

const baseCall = { id: 'call-1', runId: 'run-1', attemptId: 'attempt-sage', agentId: 'worker-a', status: 'returned', phase: 'response', createdAt: '2026-10-08T12:00:00Z', completedAt: '2026-10-08T12:00:01Z', messages: [], response: { content: 'Sage revised answer', model: 'openai/gpt-oss-120b', tool_calls: [] }, toolResults: [] };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('names the author and links only an exact saved peer reply to its attempt', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, messages: [{ role: 'user', content: JSON.stringify({ peerRequestId: 'req-1', peerResponse: 'Exact peer assessment' }) }] }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} onOpenAttempt={open} />);
  expect(await screen.findByRole('heading', { name: 'Model response' })).toBeInTheDocument();
  expect(screen.getByText('Sage revised answer')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Agent Mira reply used by Agent Sage' })).toBeInTheDocument();
  expect(screen.getByText('Exact peer assessment')).toBeInTheDocument();
  expect(screen.getByText('Saved peer request req-1')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open Mira attempt' }));
  expect(open).toHaveBeenCalledWith('attempt-peer');
  await user.click(screen.getByText('Exact call metadata and raw record'));
  expect(screen.getByText('Full raw call JSON')).toBeInTheDocument();
});

it('uses the backend trace for author, exact output path and peer record pointers', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, messages: [], trace: {
    basis: 'linked_saved_records',
    author: { agentId: 'worker-a', name: 'Canonical Sage', role: 'synthesis', taskId: 'task-a', attemptId: 'attempt-sage', runId: 'run-1' },
    outputs: [{ authoredByAgentId: 'worker-a', recordPath: 'response.content' }],
    peerInputs: [{ inputRecordPath: 'messages[0].content', peerRequestId: 'req-1', peerResponse: 'Exact peer assessment', linkStatus: 'linked', requestMessageId: 'request-message', responseMessageId: 'reply-message', peerAgentId: 'worker-b', peerAgentName: 'Canonical Mira', sourceAttemptId: 'attempt-peer', sourceAttemptResolution: 'unique' }],
  } }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} onOpenAttempt={open} />);
  expect(await screen.findByRole('heading', { name: 'Model response' })).toBeInTheDocument();
  expect(screen.getByText('Response path')).not.toBeVisible();
  await user.click(screen.getByText('Exact call metadata and raw record'));
  expect(screen.getByText('Response path')).toBeVisible();
  expect(screen.getByText('response.content')).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Agent Canonical Mira reply used by Agent Canonical Sage' })).toBeInTheDocument();
  expect(screen.getByText('Request record request-message · reply record reply-message')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open Canonical Mira attempt' }));
  expect(open).toHaveBeenCalledWith('attempt-peer');
});

it('does not infer a peer link from names in unrelated response text', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, response: { ...baseCall.response, content: 'Mira is an unrelated name here.' } }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText('Mira is an unrelated name here.')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /reply used by/ })).not.toBeInTheDocument();
});

it('shows a quota wait without inventing a response and links the saved retry', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [
    { ...baseCall, status: 'rate_limited', phase: 'quota_wait', response: { error: 'no retry was made' } },
    { ...baseCall, id: 'call-2', retryOfCallId: 'call-1', response: { ...baseCall.response, content: 'Later answer' } },
  ] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const articles = await screen.findAllByRole('article');
  expect(within(articles[0]).getByText('Quota wait: no model response was returned for this call.')).toBeInTheDocument();
  expect(within(articles[0]).queryByRole('heading', { name: 'Model response' })).not.toBeInTheDocument();
  expect(within(articles[0]).getByText('Retried in Call 2; inspect that call for its separate outcome.')).toBeInTheDocument();
  expect(within(articles[1]).getByText('Later answer')).toBeInTheDocument();
});

it('shows only safe exact provider validation fields for a rejected call', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{
    ...baseCall, status: 'failed', response: { content: null, tool_calls: [] },
    providerValidation: { kind: 'missing_required_argument', function_name: 'search_scholarly', field: 'query' },
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText(/Provider tool validation: search_scholarly lacked required query/)).toBeVisible();
  expect(screen.getByText(/provider rejected this call before the host tool ran/)).toBeVisible();
});

it('shows one rejected action with its exact arguments, host return and recovery without assigning an unmatched result', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'tool-a', function: { name: 'inspect_hit', arguments: '{"hit_id":"saved-hit"}' } }] },
    toolResults: [{ tool_call_id: 'tool-a', name: 'inspect_hit', content: JSON.stringify({ status: 'rejected', reason: 'Saved source not permitted', next_action: 'Choose another saved hit' }) },
      { tool_call_id: 'unmatched', name: 'other_tool', content: JSON.stringify({ status: 'inspected' }) }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const rejected = (await screen.findByText('Tried to inspect a saved search result')).closest('.live-call-tool')!;
  expect(within(rejected as HTMLElement).getByText('Saved search result: saved-hit')).toBeVisible();
  expect(within(rejected as HTMLElement).getByText(/"hit_id": "saved-hit"/)).toBeVisible();
  expect(within(rejected as HTMLElement).getByText(/"next_action":"Choose another saved hit"/)).toBeVisible();
  expect(screen.getByText('Saved source not permitted')).toBeVisible();
  expect(screen.getByText('Choose another saved hit')).toBeVisible();
  expect(screen.getByText('Tool rejected')).toBeVisible();
  expect(screen.getByText('Unlinked host return 2')).toBeVisible();
  expect(screen.getByText(/No matching requested function is linked by both saved call ID and name/)).toBeVisible();
  await user.click(within(rejected as HTMLElement).getByText('Exact tool identity'));
  expect(within(rejected as HTMLElement).getByText(/Function: inspect_hit · call ID: tool-a · host result 1/)).toBeVisible();
});

it('links only an exact call ID and function name when several functions were requested', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'call-a', function: { name: 'search_scholarly', arguments: '{"query":"saved"}' } },
      { id: 'call-b', function: { name: 'read_source_window', arguments: '{"copy_id":"saved-copy"}' } },
    ] },
    toolResults: [
      { tool_call_id: 'call-a', name: 'search_scholarly', content: '{"status":"completed"}' },
      { tool_call_id: 'call-b', name: 'different_function', content: '{"status":"rejected"}' },
    ],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const linkedRequest = (await screen.findByText('Searched scholarly sources')).closest('.live-call-tool')!;
  const unlinkedRequest = screen.getByText('Requested: Read a saved source excerpt').closest('.live-call-tool')!;
  expect(within(linkedRequest as HTMLElement).getByText(/"query": "saved"/)).toBeVisible();
  expect(within(linkedRequest as HTMLElement).getByText('{"status":"completed"}')).toBeVisible();
  expect(within(unlinkedRequest as HTMLElement).getByText('No saved host result is linked yet. This does not establish whether the tool ran.')).toBeVisible();
  expect(within(unlinkedRequest as HTMLElement).queryByText('Host return')).not.toBeInTheDocument();
  expect(screen.getByText('Unlinked host return 2')).toBeVisible();
  expect(screen.getByText('No matching requested function is linked by both saved call ID and name.')).toBeVisible();
});

it('leaves a reused tool-call identity ambiguous instead of assigning a result by position', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'reused-id', function: { name: 'inspect_hit', arguments: '{"hit_id":"one"}' } },
      { id: 'reused-id', function: { name: 'inspect_hit', arguments: '{"hit_id":"two"}' } },
    ] },
    toolResults: [{ tool_call_id: 'reused-id', name: 'inspect_hit', content: '{"status":"inspected"}' }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findAllByText('Saved request or result identity is ambiguous; no unique host result can be linked.')).toHaveLength(2);
  expect(screen.getByText('Saved request or result identity is ambiguous; no unique requested function can be linked.')).toBeVisible();
  expect(screen.getByText('Unlinked host return 1')).toBeVisible();
  expect(screen.queryByText('Inspected a saved search result')).not.toBeInTheDocument();
});

it('reads available provider reasoning only on demand from the exact call and distinguishes prepared calls', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [
    { ...baseCall, returnedReasoning: { availability: 'restricted' } },
    { ...baseCall, id: 'call-prepared', status: 'prepared', response: null, returnedReasoning: { availability: 'absent' } },
  ] });
  vi.mocked(getLiveReasoning).mockResolvedValue({ runId: 'run-1', callId: 'call-1', attemptId: 'attempt-sage', agentId: 'worker-a', status: 'available', origin: 'provider_returned', text: 'Synthetic saved provider text.', redacted: false });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText('No model response was returned for this prepared call.')).toBeVisible();
  expect(getLiveReasoning).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await screen.findByText('Synthetic saved provider text.')).toBeVisible();
  expect(getLiveReasoning).toHaveBeenCalledWith('run-1', 'call-1');
});

it('keeps inspected calls open across a snapshot refresh but hides them on a run switch', async () => {
  const user = userEvent.setup();
  const first = { ...baseCall, returnedReasoning: { availability: 'restricted' },
    toolResults: [{ content: JSON.stringify({ status: 'rejected', reason: 'Synthetic rejection' }) }] };
  vi.mocked(getLiveCalls).mockResolvedValueOnce({ records: [first] })
    .mockImplementationOnce(() => new Promise(() => {}))
    .mockImplementationOnce(() => new Promise(() => {}));
  vi.mocked(getLiveReasoning).mockResolvedValue({ runId: 'run-1', callId: 'call-1', attemptId: 'attempt-sage', agentId: 'worker-a', status: 'available', origin: 'provider_returned', text: 'Synthetic private text.', redacted: false });
  const view = render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  await screen.findByText('Synthetic rejection');
  await user.click(screen.getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await screen.findByText('Synthetic private text.')).toBeVisible();

  view.rerender(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-2" snapshot={saved} />);
  expect(screen.getByText(/prior snapshot until this check completes/)).toBeVisible();
  expect(screen.getByText('Synthetic private text.')).toBeVisible();
  expect(screen.getByText(/"status":"rejected"/)).toBeVisible();

  view.rerender(<LiveCallInspector runId="run-other" attemptId="attempt-sage" snapshotId="snapshot-other" snapshot={saved} />);
  expect(screen.queryByText('Synthetic private text.')).not.toBeInTheDocument();
  expect(screen.queryByText('Synthetic rejection')).not.toBeInTheDocument();
});

it('distinguishes a missing tool-content field from a saved empty return', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, toolResults: [
    { name: 'read_context_record', tool_call_id: 'unlinked-a' },
    { name: 'read_context_record', tool_call_id: 'unlinked-b', content: '' },
  ] }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText('No tool return content field was saved for this record.')).toBeVisible();
  expect(screen.getByText('A tool return was saved; no separate outcome status was recorded here.')).toBeVisible();
  expect(screen.getByText('"" (empty saved string)')).toBeInTheDocument();
});

it('states when a requested function has no linked host result without inferring execution', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'tool-pending', function: { name: 'read_source_window', arguments: '{"copy_id":"saved-copy"}' } }] },
    toolResults: [],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText('No saved host result is linked yet. This does not establish whether the tool ran.')).toBeVisible();
  expect(screen.getByText('Requested: Read a saved source excerpt')).toBeVisible();
  expect(screen.getByText(/"copy_id": "saved-copy"/)).toBeVisible();
  expect(screen.queryByText('Host return')).not.toBeInTheDocument();
});

it('uses an exact source ID and version for a readable title, without substituting a nearby saved source', async () => {
  const snapshot = { ...saved, sources: [
    { ...saved.sources[0], id: 'source-one', version: 2, title: 'Exact saved study' },
    { ...saved.sources[0], id: 'source-one', version: 3, title: 'Different version title' },
  ] } as ResearchSnapshot;
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'window-one', function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":2,"char_offset":40,"char_limit":200}' } },
      { id: 'window-unknown', function: { name: 'read_source_window', arguments: '{"source_id":"source-missing","source_version":2,"char_offset":0,"char_limit":200}' } },
    ] },
    toolResults: [{ tool_call_id: 'window-one', name: 'read_source_window', content: '{"status":"completed","text":"Saved excerpt"}' }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={snapshot} />);
  const read = (await screen.findByText('Read a saved source excerpt')).closest('.live-call-tool')!;
  expect(within(read as HTMLElement).getByText('Saved source: Exact saved study')).toBeVisible();
  expect(within(read as HTMLElement).getByText('Saved source copy version').nextElementSibling).toHaveTextContent('2');
  expect(within(read as HTMLElement).getByText('Start at character').nextElementSibling).toHaveTextContent('40');
  expect(within(read as HTMLElement).getByText('Requested maximum passage length').nextElementSibling).toHaveTextContent('200 characters');
  expect(within(read as HTMLElement).getByText('Exact source-read arguments')).toBeVisible();
  expect(within(read as HTMLElement).getByText(/"source_id": "source-one"/)).not.toBeVisible();
  expect(within(read as HTMLElement).getByText('{"status":"completed","text":"Saved excerpt"}')).toBeVisible();
  expect(screen.queryByText(/Different version title/)).not.toBeInTheDocument();
  expect(screen.getByText('Saved source copy: source-missing (title not verified here)')).toBeVisible();
});

it('shows an exact returned source excerpt and limits without treating a missing status as missing content', async () => {
  const user = userEvent.setup();
  const snapshot = { ...saved, sources: [{ ...saved.sources[0], id: 'source-one', version: 2, title: 'Exact saved study' }] } as ResearchSnapshot;
  const returned = { copy_ref: { kind: 'source_copy', id: 'source-one', version: 2 }, returned_chars: 39,
    effective_char_limit: 200, remaining_in_block_chars: 31, next_cursor: { block_offset: 3, char_offset: 79 },
    passages: [{ locator: 'block 3', text_preview: 'Synthetic line one.\nSynthetic line two.' }] };
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'window-one', function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":2,"block_offset":3,"char_offset":40,"char_limit":200}' } }] },
    toolResults: [{ tool_call_id: 'window-one', name: 'read_source_window', content: JSON.stringify(returned) }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={snapshot} />);
  const read = (await screen.findByText('Read a saved source excerpt')).closest('.live-call-tool') as HTMLElement;
  expect(within(read).getByText('Saved source: Exact saved study')).toBeVisible();
  expect(within(read).getByText('Saved source copy version').nextElementSibling).toHaveTextContent('2');
  expect(within(read).getByText('Section offset').nextElementSibling).toHaveTextContent('3');
  expect(within(read).getByText('Start at character').nextElementSibling).toHaveTextContent('40');
  expect(within(read).getByText('Requested maximum passage length').nextElementSibling).toHaveTextContent('200 characters');
  expect(within(read).getByText(/Offsets start at 0 in the saved text/)).toBeVisible();
  expect(within(read).getByText(/39 characters of saved text · 200-character effective limit · 31 characters remaining/)).toBeVisible();
  expect(within(read).getByText('Returned passage 1 · Evidence number unavailable · block 3')).toBeVisible();
  expect(within(read).getByText('This returned passage does not match an exact numbered excerpt in the selected snapshot.')).toBeVisible();
  expect(within(read).getByText('Synthetic line one. Synthetic line two.', { selector: 'blockquote' })).toBeVisible();
  expect(within(read).getByText('Next saved reading position: section offset 3, character 79.')).toBeVisible();
  expect(within(read).getByText(/"block_offset": 3/)).not.toBeVisible();
  await user.click(within(read).getByText('Exact source-read arguments'));
  expect(within(read).getByText(/"block_offset": 3/)).toBeVisible();
  expect(within(read).getByText('A tool return was saved; no separate outcome status was recorded here.')).toBeVisible();
  expect(within(read).queryByText('Host returned from: Read a saved source excerpt')).not.toBeInTheDocument();
  expect(within(read).getByText('Exact saved host return JSON')).toBeVisible();
  expect(within(read).getByText(JSON.stringify(returned))).not.toBeVisible();
  await user.click(within(read).getByText('Exact saved host return JSON'));
  expect(within(read).getByText(JSON.stringify(returned))).toBeVisible();
});

it('keeps unknown source-window return shapes unclassified and does not borrow an unmatched excerpt', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'window-one', function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":2}' } }] },
    toolResults: [
      { tool_call_id: 'window-one', name: 'read_source_window', content: '{"unrecognized":"shape"}' },
      { tool_call_id: 'another-window', name: 'read_source_window', content: '{"returned_chars":5,"passages":[{"text_preview":"Other excerpt"}]}' },
    ],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const request = (await screen.findByText('Host returned from: Read a saved source excerpt')).closest('.live-call-tool') as HTMLElement;
  expect(within(request).getByText('Host return (unclassified format)')).toBeVisible();
  expect(within(request).getByText('{"unrecognized":"shape"}')).toBeVisible();
  expect(within(request).queryByText('Other excerpt')).not.toBeInTheDocument();
  expect(screen.getByText('Unlinked host return 2')).toBeVisible();
});

it('does not label a returned excerpt as the requested source when its saved copy reference disagrees', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'window-one', function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":2}' } }] },
    toolResults: [{ tool_call_id: 'window-one', name: 'read_source_window', content: JSON.stringify({
      copy_ref: { kind: 'source_copy', id: 'another-copy', version: 2 }, returned_chars: 5,
      passages: [{ text_preview: 'Other excerpt' }],
    }) }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const request = (await screen.findByText('Host returned from: Read a saved source excerpt')).closest('.live-call-tool') as HTMLElement;
  expect(within(request).getByText('Host return (unclassified format)')).toBeVisible();
  expect(within(request).queryByText('Saved excerpt 1')).not.toBeInTheDocument();
  expect(within(request).getByText(/another-copy/)).toBeVisible();
});

it('uses the Evidence excerpt numbers from exact saved passage identity and explains overlapping selections', async () => {
  const secondSpan = `window-${'b'.repeat(32)}`;
  const seventhSpan = `window-${'a'.repeat(32)}`;
  const passages = Array.from({ length: 8 }, (_, index) => ({ spanId: `other-${index}`, locator: `other-${index}`, text: 'x' }));
  passages[1] = { spanId: secondSpan, locator: 'abstract; chars=10:16', text: 'abcdef' };
  passages[6] = { spanId: seventhSpan, locator: 'abstract; chars=10:20', text: 'abcdefghij' };
  passages[7] = { spanId: 'conflicting-later-excerpt', locator: 'abstract; chars=10:20', text: 'zzzzzzzzzz' };
  const snapshot = { ...saved, sources: [{ ...saved.sources[0], id: 'source-one', copyId: 'source-one', version: 1, passages }] } as ResearchSnapshot;
  const tool = (id: string) => ({ id, function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":1,"block_offset":0,"char_offset":10,"char_limit":20}' } });
  const result = (id: string, spanId: string, locator: string, text: string) => {
    const passageRef = { kind: 'source_passage', id: `source-one:${spanId}`, version: 1 };
    return { tool_call_id: id, name: 'read_source_window', content: JSON.stringify({
      copy_ref: { kind: 'source_copy', id: 'source-one', version: 1 }, returned_chars: text.length,
      passage_refs: [passageRef], passages: [{ passage_ref: passageRef, span_id: spanId, locator, text_preview: text }],
    }) };
  };
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [
    { ...baseCall, response: { ...baseCall.response, tool_calls: [tool('window-seven')] }, toolResults: [result('window-seven', seventhSpan, passages[6].locator, passages[6].text)] },
    { ...baseCall, id: 'call-two', response: { ...baseCall.response, tool_calls: [tool('window-two')] }, toolResults: [result('window-two', secondSpan, passages[1].locator, passages[1].text)] },
  ] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={snapshot} />);
  const calls = await screen.findAllByRole('article');
  expect(within(calls[0]).getByText('Evidence excerpt 7 · abstract; chars=10:20')).toBeVisible();
  expect(within(calls[0]).getByText('Contains Evidence excerpt 2 and adds 4 characters.')).toBeVisible();
  expect(within(calls[0]).getByText('Different wording at an overlapping saved location: Evidence excerpt 8. Treat these as separate excerpts.')).toBeVisible();
  expect(within(calls[1]).getByText('Evidence excerpt 2 · abstract; chars=10:16')).toBeVisible();
  expect(within(calls[1]).getByText('A shorter selection within Evidence excerpt 7; no additional text in that range.')).toBeVisible();
  expect(within(calls[1]).getByText('Different wording at an overlapping saved location: Evidence excerpt 8. Treat these as separate excerpts.')).toBeVisible();
});

it('withholds Evidence numbering when the passage reference or saved wording conflicts', async () => {
  const spanId = `window-${'a'.repeat(32)}`;
  const snapshot = { ...saved, sources: [{ ...saved.sources[0], id: 'source-one', copyId: 'source-one', version: 1,
    passages: [{ spanId, locator: 'abstract; chars=10:20', text: 'abcdefghij' }] }] } as ResearchSnapshot;
  const wrongRef = { kind: 'source_passage', id: 'source-one:wrong-window', version: 1 };
  const exactRef = { kind: 'source_passage', id: `source-one:${spanId}`, version: 1 };
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [{ id: 'window-one', function: { name: 'read_source_window', arguments: '{"source_id":"source-one","source_version":1}' } }] },
    toolResults: [{ tool_call_id: 'window-one', name: 'read_source_window', content: JSON.stringify({
      copy_ref: { kind: 'source_copy', id: 'source-one', version: 1 }, returned_chars: 20,
      passage_refs: [wrongRef, exactRef], passages: [
        { passage_ref: wrongRef, span_id: spanId, locator: 'abstract; chars=10:20', text_preview: 'abcdefghij' },
        { passage_ref: exactRef, span_id: spanId, locator: 'abstract; chars=10:20', text_preview: 'different!' },
      ],
    }) }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={snapshot} />);
  expect(await screen.findAllByText(/Evidence number unavailable · abstract; chars=10:20/)).toHaveLength(2);
  expect(screen.queryByText(/^Evidence excerpt 1 ·/)).not.toBeInTheDocument();
});

it('summarizes only verified inventory and capability fields from their exact linked returns', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'inventory-one', function: { name: 'read_source_inventory', arguments: '{"inventory_id":"inventory-one","inventory_version":1,"cursor":0,"limit":10}' } },
      { id: 'capabilities-one', function: { name: 'discover_capabilities', arguments: '{}' } },
    ] },
    toolResults: [
      { tool_call_id: 'inventory-one', name: 'read_source_inventory', content: JSON.stringify({ schema: 'ora.source_inventory_page.v1', total: 2, next_cursor: 1, items: [{ ref: { kind: 'source_copy', id: 'copy-one', version: 3 }, title: 'Saved title', model_input_status: 'permitted' }] }) },
      { tool_call_id: 'capabilities-one', name: 'discover_capabilities', content: JSON.stringify({ granted: { read_source_window: 'Read an exact saved copy' }, exposed: ['discover_capabilities'], next_action: 'Request access' }) },
    ],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const inventory = (await screen.findByText('Listed saved sources')).closest('.live-call-tool') as HTMLElement;
  expect(within(inventory).getByText(/1 on this page · 2 total/)).toBeVisible();
  expect(within(inventory).getByText('1. Saved title · source_copy · copy-one · version 3 · permitted')).toBeVisible();
  expect(within(inventory).getByText('Next saved page cursor: 1.')).toBeVisible();
  const capabilities = screen.getByText('Checked available tools').closest('.live-call-tool') as HTMLElement;
  expect(within(capabilities).getByText(/1 granted · 1 exposed in this call/)).toBeVisible();
  expect(within(capabilities).getByText('read_source_window · Read an exact saved copy')).toBeVisible();
  expect(within(capabilities).getByText('Request access')).toBeVisible();
});

it('names a peer direction only for an exact saved instance ID and keeps delivery separate', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'peer-exact', function: { name: 'peer_request', arguments: '{"target_instance_id":"worker-b","question":"Check this claim"}' } },
      { id: 'peer-alias', function: { name: 'peer_request', arguments: '{"target_instance_id":"Mira","question":"Check another claim"}' } },
    ] },
    toolResults: [{ tool_call_id: 'peer-exact', name: 'peer_request', content: '{"status":"requested","request_id":"request-one"}' }],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const exact = (await screen.findByText('Saved a peer request')).closest('.live-call-tool')!;
  expect(within(exact as HTMLElement).getByText('Requested peer direction: Sage → Mira')).toBeVisible();
  expect(within(exact as HTMLElement).getByText('{"status":"requested","request_id":"request-one"}')).toBeVisible();
  expect(within(exact as HTMLElement).queryByText(/delivered|answered/i)).not.toBeInTheDocument();
  expect(screen.getByText('Requested peer: Mira (identity not verified here)')).toBeVisible();
});

it('does not turn an unclassified saved return or duplicate result into a completed action', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall,
    response: { ...baseCall.response, tool_calls: [
      { id: 'unknown-one', function: { name: 'unrecognized_action', arguments: '{"target":"one"}' } },
      { id: 'duplicate', function: { name: 'read_context_record', arguments: '{"kind":"task_control","id":"control-one","version":4}' } },
    ] },
    toolResults: [
      { tool_call_id: 'unknown-one', name: 'unrecognized_action', content: 'Saved plain text' },
      { tool_call_id: 'duplicate', name: 'read_context_record', content: '{"status":"completed"}' },
      { tool_call_id: 'duplicate', name: 'read_context_record', content: '{"status":"rejected"}' },
    ],
  }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const unknown = (await screen.findByText('Host returned from unrecognized_action')).closest('.live-call-tool')!;
  expect(within(unknown as HTMLElement).getByText('Saved plain text')).toBeVisible();
  expect(screen.getAllByText('Saved request or result identity is ambiguous; no unique requested function can be linked.')).toHaveLength(2);
  expect(screen.queryByText('Opened a saved task record')).not.toBeInTheDocument();
});
