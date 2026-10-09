/* Exercise reader-facing formatting and source controls without allowing report
   text to execute HTML, load images, or invoke native commands. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ReportMarkdown } from './ReportMarkdown';

afterEach(() => cleanup());

it('renders a longer report with headings, paragraphs, lists, a table and code', () => {
  const text = '# Findings\n\nThe screened proportion is **25%**.\n\nThe identified proportion is *20.8%*.\n\n- Preserve counts.\n- Explain both groups.\n  - Unique records were screened.\n\n1. Inspect the evidence.\n2. Compare the calculations.\n\n| Group | Calculation |\n| --- | --- |\n| Screened | `5 / 20` |\n| Identified | `5 / 24` |\n\n## Remaining uncertainty\n\n> This is simulated evidence.\n\n```text\nDo not interpret **markers** in code.\n```';
  const { container } = render(<ReportMarkdown text={text} />);
  expect(screen.getByRole('heading', { name: 'Findings', level: 4 })).toBeVisible();
  expect(screen.getByText('25%', { selector: 'strong' })).toBeVisible();
  expect(screen.getByText('20.8%', { selector: 'em' })).toBeVisible();
  expect(screen.getAllByRole('list')).toHaveLength(3);
  expect(screen.getByText('Unique records were screened.').closest('ul')?.parentElement?.tagName).toBe('LI');
  expect(within(screen.getByRole('table')).getByText('5 / 24', { selector: 'code' })).toBeVisible();
  expect(container.querySelector('blockquote')).toHaveTextContent('This is simulated evidence.');
  expect(container.querySelector('pre')).toHaveTextContent('Do not interpret **markers** in code.');
});

it('links only exact resolved source references and leaves unknown identifiers visible', async () => {
  const open = vi.fn();
  const { container } = render(<ReportMarkdown text={'Answer. **Citations:** `source-A::span-1`; source-A::span-1; [source-A::span-1]. Unresolved: `source-B::span-2`. Partial token: source-A::span-10.'} sourceReferences={[{ sourceId: 'source-A', spanId: 'span-1', label: '1' }]} onOpenSourceReference={open} />);
  const citations = screen.getAllByRole('button', { name: 'Open cited passage 1' });
  expect(citations).toHaveLength(3);
  expect(container.textContent).not.toContain('[[1]]');
  await userEvent.click(citations[0]);
  expect(open).toHaveBeenCalledExactlyOnceWith('source-A', 'span-1');
  expect(screen.getByText('source-B::span-2', { selector: 'code' })).toBeVisible();
  expect(screen.getByText(/source-A::span-10/)).toBeVisible();
});

it('keeps active HTML and command links inert and does not load embedded images', () => {
  const { container } = render(<ReportMarkdown text={'<script>window.attack=true</script>\n\n<img src="https://example.test/tracker" onerror="alert(1)">\n\n[Unsafe](javascript:alert) [Native](tauri://localhost) [Allowed](https://example.test/paper)\n\n![Tracking pixel](https://example.test/pixel)'} />);
  expect(container.querySelector('script,img,iframe')).toBeNull();
  expect(container.textContent).toContain('<script>window.attack=true</script>');
  expect(container.textContent).toContain('[Unsafe](javascript:alert)');
  expect(screen.getAllByRole('link')).toHaveLength(1);
  expect(screen.getByRole('link', { name: 'Allowed' })).toHaveAttribute('rel', 'noopener noreferrer');
  expect(container.textContent).toContain('![Tracking pixel](https://example.test/pixel)');
});

it('keeps long paragraphs intact and does not mistake escaped pipes or code for table columns', () => {
  const paragraph = 'A saved finding with its original qualification. '.repeat(100);
  render(<ReportMarkdown text={`${paragraph}\n\n| Field | Value |\n| --- | --- |\n| Escaped \\| pipe | \`a|b\` |`} />);
  expect(screen.getByText(paragraph.trim())).toBeVisible();
  expect(within(screen.getByRole('table')).getAllByRole('cell')).toHaveLength(2);
  expect(screen.getByText('a|b', { selector: 'code' })).toBeVisible();
});

it('preserves a meaningful trailing hash and shallow list continuation text', () => {
  render(<ReportMarkdown text={'# C#\n\n- Count\n one caveat\n\n## Next section ##'} />);
  expect(screen.getByRole('heading', { name: 'C#' })).toBeVisible();
  expect(screen.getByText(/Count\s+one caveat/)).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Next section' })).toBeVisible();
});

it('does not link a structured reference inside a different dotted identifier', () => {
  render(<ReportMarkdown text={'A1::span.extra and .A1::span are distinct tokens. A1::span.'} sourceReferences={[{ sourceId: 'A1', spanId: 'span', label: '1' }]} onOpenSourceReference={() => {}} />);
  expect(screen.getAllByRole('button')).toHaveLength(1);
  expect(screen.getByText(/A1::span.extra and .A1::span/)).toBeVisible();
});
