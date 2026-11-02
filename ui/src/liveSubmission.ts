/* Keep command acceptance separate from later viewer reads.
   The form owns the frozen request; this reducer only represents its observed
   outcome and never creates a new request identity or retries a write. */
import type { LiveReceipt } from './live';

type Submission = {
  submitting: boolean; receipt: LiveReceipt | null; agentIds: string[]; requiredAgentIds: string[];
  error: string | null; errorKind: 'unconfirmed' | 'view_unavailable';
};
type Action = { type: 'begin' | 'reset' } | { type: 'accepted'; receipt: LiveReceipt; agentIds: string[]; requiredAgentIds: string[] }
  | { type: 'failed'; message: string } | { type: 'settled' };
export const initialSubmission: Submission = { submitting: false, receipt: null, agentIds: [], requiredAgentIds: [], error: null, errorKind: 'unconfirmed' };
export function submissionReducer(state: Submission, action: Action): Submission {
  switch (action.type) {
    case 'begin': return { ...initialSubmission, submitting: true };
    case 'reset': return initialSubmission;
    case 'accepted': return { ...state, receipt: action.receipt, agentIds: action.agentIds, requiredAgentIds: action.requiredAgentIds };
    case 'failed': return { ...state, error: action.message, errorKind: state.receipt ? 'view_unavailable' : 'unconfirmed' };
    case 'settled': return { ...state, submitting: false };
  }
}
