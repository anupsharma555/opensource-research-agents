import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { RecordedText } from './RecordedText';

afterEach(() => cleanup());

describe('recorded text formatting', () => {
  it('renders saved arithmetic, code and explicit citation and limitation sections', () => {
    render(<RecordedText text={'**Result:** **5 of 20** screened records were included: `5/20 = 25%`.\n\nOut of 24 identified records, **5/24 = 20.8%**. **Citations:** `source-A:span-2`. **Limitations:** Methods were not supplied.'} />);
    expect(screen.getByText('5 of 20', { selector: 'strong' })).toBeVisible();
    expect(screen.getByText('5/20 = 25%', { selector: 'code' })).toBeVisible();
    const citations = screen.getByRole('region', { name: 'Citations' });
    expect(within(citations).getByText('source-A:span-2', { selector: 'code' })).toBeVisible();
    const limitations = screen.getByRole('region', { name: 'Limitations' });
    expect(within(limitations).getByText(/Methods were not supplied/)).toBeVisible();
    expect(screen.getByText('5/24 = 20.8%', { selector: 'strong' })).toBeVisible();
    expect(document.querySelector('a')).toBeNull();
  });

  it('renders HTML as text and keeps malformed or unsupported markers readable', () => {
    const text = '<script>window.attack = true</script> <img src=x onerror=alert(1)> **unfinished `orphan *italic* [link](https://example.test)';
    const { container } = render(<RecordedText text={text} />);
    expect(container.querySelector('script, img, a')).toBeNull();
    expect(container.textContent).toContain('<script>window.attack = true</script>');
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(container.textContent).toContain('**unfinished `orphan *italic* [link](https://example.test)');
  });

  it('keeps label-like text inside inline code literal and supports singular limitation', () => {
    render(<RecordedText text={'`**Citations:**` is literal. **Limitation:** One saved source.'} />);
    expect(screen.getByText('**Citations:**', { selector: 'code' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Citations' })).not.toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Limitation' })).getByText(/One saved source/)).toBeVisible();
  });
});
