import {
  analyzeTextContent,
  isSpamText,
  sanitizeText,
  validateTextContent,
  validateTextLength,
} from '../../../../src/utils/ai/textValidation';

describe('textValidation utilities', () => {
  it('enforces maximum text length', () => {
    expect(() => validateTextLength('hello', 5)).not.toThrow();
    expect(() => validateTextLength('toolong', 3)).toThrow(
      'Text length (7 characters) exceeds maximum allowed length (3 characters)',
    );
  });

  it('enforces meaningful content', () => {
    expect(() => validateTextContent(' hello ', 3)).not.toThrow();
    expect(() => validateTextContent('   ', 1)).toThrow('Text must be at least 1 characters long');
  });

  it('sanitizes noisy input without stripping basic punctuation', () => {
    expect(sanitizeText('  Hello   there!!! $$%  ')).toBe('Hello there!!! ');
  });

  it('flags common spam patterns', () => {
    expect(isSpamText('AAAAAAAAAAAA')).toBe(true);
    expect(isSpamText('visit http://example.com now')).toBe(true);
    expect(isSpamText('normal message')).toBe(false);
  });

  it('analyzes text content counts', () => {
    expect(analyzeTextContent('Hello world! Visit https://example.com 😀')).toEqual({
      wordCount: 5,
      characterCount: 41,
      sentenceCount: 2,
      hasEmojis: true,
      hasUrls: true,
    });
  });
});
