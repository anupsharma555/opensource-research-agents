/* Verify that visible agent identity comes from saved names and IDs.
   Badge color remains tied to identity and never substitutes for readable text. */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AgentBadge } from './AgentBadge';

describe('AgentBadge', () => {
  it('shows a saved name and initial with a stable identity tone', () => {
    const { rerender } = render(<AgentBadge name="Sage" agentId="agent-synthesis" />);
    const badge = screen.getByText('Agent Sage').closest('.agent-badge')!;
    expect(badge.querySelector('.agent-badge-initial')).toHaveTextContent('S');
    expect(badge.querySelector('.agent-badge-initial')).toHaveAttribute('aria-hidden', 'true');
    const tone = [...badge.classList].find(value => value.startsWith('agent-badge-tone-'));
    rerender(<AgentBadge name="Saved display name changed" agentId="agent-synthesis" />);
    expect(screen.getByText('Agent Saved display name changed').closest('.agent-badge')).toHaveClass(tone!);
  });

  it('states when an agent name is unavailable instead of inventing one', () => {
    render(<AgentBadge agentId="agent-unknown" />);
    expect(screen.getByText('Agent identity unavailable (agent-unknown)')).toBeInTheDocument();
    expect(screen.getByText('?')).toHaveAttribute('aria-hidden', 'true');
  });
});
