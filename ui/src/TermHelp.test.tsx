/* Verify that definitions are reachable by mouse and keyboard without navigation. */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { AtlasTerms, TermHelp } from './TermHelp';

it('opens on first click, explains board versus messages, and closes with Escape', async () => {
  const user = userEvent.setup();
  render(<TermHelp term="shared work board" />);
  const help = screen.getByRole('button', { name: 'What does shared work board mean?' });
  await user.click(help);
  expect(screen.getByRole('tooltip')).toHaveTextContent('A board entry does not establish that a message was sent');
  expect(help).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  expect(help).toHaveFocus();
});

it('supports hover and keyboard focus, and dismisses when focus leaves', async () => {
  const user = userEvent.setup();
  render(<><TermHelp term="work step" /><button>Next control</button></>);
  const help = screen.getByRole('button', { name: 'What does work step mean?' });
  await user.hover(help);
  expect(screen.getByRole('tooltip')).toHaveTextContent('one agent on one task in a run');
  await user.unhover(help);
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  await user.tab();
  expect(screen.getByRole('tooltip')).toBeInTheDocument();
  await user.tab();
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Next control' })).toHaveFocus();
});

it('explains the run, task and step hierarchy without making a prepared call or saved report proof of completion', async () => {
  const user = userEvent.setup();
  render(<AtlasTerms />);
  await user.click(screen.getByText('What the workspace terms mean'));
  expect(screen.getByText('run').closest('div')).toHaveTextContent('same ID through waits and restarts');
  expect(screen.getByText('task').closest('div')).toHaveTextContent('One run can contain several tasks');
  expect(screen.getByText('step / work step').closest('div')).toHaveTextContent('not a separate research run');
  expect(screen.getByText('prepared request').closest('div')).toHaveTextContent('Prepared does not mean sent');
  expect(screen.getByText('accepted report').closest('div')).toHaveTextContent('does not mean its scientific claims have been independently checked');
  expect(screen.getByText('host / system').closest('div')).toHaveTextContent('Agents make the scientific judgments');
  expect(screen.getByText('tool').closest('div')).toHaveTextContent('not the final research answer');
  expect(screen.queryByText('work step', { selector: 'dt' })).not.toBeInTheDocument();
  expect(screen.queryByText('system', { selector: 'dt' })).not.toBeInTheDocument();
});
