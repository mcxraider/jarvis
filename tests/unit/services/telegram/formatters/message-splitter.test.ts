import { splitMessage } from '../../../../../src/services/telegram/formatters/message-splitter';

describe('message-splitter', () => {
  it('returns a single chunk when message fits', () => {
    const text = 'Hello [link](https://example.com) world';
    expect(splitMessage(text, 100)).toEqual([text]);
  });

  it('does not split inside a Markdown link', () => {
    // 40 chars of padding, then a link that would straddle the split at maxLength=60
    const text = 'A'.repeat(40) + ' [label](https://example.com/path) end';
    const chunks = splitMessage(text, 60);
    // The link must not be torn apart — split should move before the '['
    for (const chunk of chunks) {
      const openBrackets = (chunk.match(/\[/g) || []).length;
      const closingParens = (chunk.match(/\)/g) || []).length;
      // If a chunk contains '[' from a link, it must also contain the closing ')'
      if (chunk.includes('[label]')) {
        expect(chunk).toContain('(https://example.com/path)');
      }
    }
    expect(chunks.join(' ')).toContain('[label](https://example.com/path)');
  });

  it('handles multiple links without splitting any', () => {
    const link1 = '[one](https://one.com)';
    const link2 = '[two](https://two.com)';
    const text = 'Start ' + link1 + ' middle ' + link2 + ' end';
    const chunks = splitMessage(text, 30);
    const rejoined = chunks.join(' ');
    expect(rejoined).toContain(link1);
    expect(rejoined).toContain(link2);
  });

  it('falls through to hard cutoff when link starts too early', () => {
    // Link starts at position 0, so moving before it would be < 30% threshold
    const text = '[very long label here](https://example.com/very/long/path/that/keeps/going)' + ' end';
    const chunks = splitMessage(text, 40);
    // Should not infinite-loop; just produces chunks
    expect(chunks.length).toBeGreaterThanOrEqual(2);
    expect(chunks.join('')).toContain('end');
  });
});
