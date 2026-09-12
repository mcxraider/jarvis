import { formatPollAsText } from '../../../../src/services/telegram/poll-content';
import { Poll } from 'telegraf/typings/core/types/typegram';

describe('formatPollAsText', () => {
  it('renders a regular poll with question, options, and collapsed metadata', () => {
    const result = formatPollAsText({
      id: '1',
      question: 'What should we have for dinner?',
      options: [
        { text: 'Sushi', voter_count: 3 },
        { text: 'Pasta', voter_count: 2 },
        { text: 'Burgers', voter_count: 1 },
      ],
      type: 'regular',
      allows_multiple_answers: true,
      is_anonymous: true,
      is_closed: false,
      total_voter_count: 6,
    } as Poll);

    expect(result).toContain('[poll] Question: What should we have for dinner?');
    expect(result).toContain('1. Sushi (3 votes)');
    expect(result).toContain('2. Pasta (2 votes)');
    expect(result).toContain('3. Burgers (1 vote)');
    expect(result).toContain('Multiple answers: yes | 6 voters | Open');
    expect(result).not.toContain('Type:');
    expect(result).not.toContain('Anonymous:');
  });

  it('renders a quiz poll with correct_option_id 0 and explanation', () => {
    const result = formatPollAsText({
      question: 'Capital of France?',
      options: [
        { text: 'Paris', voter_count: 5 },
        { text: 'London', voter_count: 1 },
      ],
      type: 'quiz',
      correct_option_id: 0,
      explanation: 'Paris is the capital of France.',
      is_anonymous: true,
    } as unknown as Poll);

    expect(result).toContain('[poll] Question: Capital of France?');
    expect(result).toContain('Correct answer: option 1');
    expect(result).toContain('Explanation: Paris is the capital of France.');
  });

  it('renders a closed poll with vote counts', () => {
    const result = formatPollAsText({
      question: 'Best day for meeting?',
      options: [
        { text: 'Monday', voter_count: 4 },
        { text: 'Tuesday', voter_count: 7 },
      ],
      is_closed: true,
      total_voter_count: 11,
    } as unknown as Poll);

    expect(result).toContain('11 voters | Closed');
    expect(result).toContain('1. Monday (4 votes)');
    expect(result).toContain('2. Tuesday (7 votes)');
  });

  it('renders a minimal poll with only question and options', () => {
    const result = formatPollAsText({
      question: 'Pick one',
      options: [{ text: 'A' }, { text: 'B' }],
    } as unknown as Poll);

    expect(result).toBeDefined();
    expect(result).toContain('[poll] Question: Pick one');
    expect(result).toContain('1. A');
    expect(result).toContain('2. B');
    expect(result).not.toContain('Type:');
    expect(result).not.toContain('Anonymous:');
  });

  it('returns undefined for missing question', () => {
    expect(formatPollAsText({ options: [{ text: 'A' }] } as unknown as Poll)).toBeUndefined();
  });

  it('returns undefined for empty string question', () => {
    expect(formatPollAsText({ question: '  ', options: [{ text: 'A' }] } as unknown as Poll)).toBeUndefined();
  });

  it('returns undefined for missing options array', () => {
    expect(formatPollAsText({ question: 'Q?' } as unknown as Poll)).toBeUndefined();
  });

  it('returns undefined for empty options array', () => {
    expect(formatPollAsText({ question: 'Q?', options: [] } as unknown as Poll)).toBeUndefined();
  });

  it('returns undefined when all options are malformed', () => {
    expect(
      formatPollAsText({ question: 'Q?', options: [{ id: 1 }, null, 42] } as unknown as Poll),
    ).toBeUndefined();
  });

  it('skips malformed options but keeps valid ones', () => {
    const result = formatPollAsText({
      question: 'Q?',
      options: [{ text: 'Valid' }, null, { id: 1 }],
    } as unknown as Poll);

    expect(result).toContain('1. Valid');
  });

  it('preserves unicode in question and options', () => {
    const result = formatPollAsText({
      question: '今晩の夕食は？🍣',
      options: [{ text: '寿司 🍣' }, { text: 'パスタ 🍝' }],
    } as unknown as Poll);

    expect(result).toContain('Question: 今晩の夕食は？🍣');
    expect(result).toContain('1. 寿司 🍣');
    expect(result).toContain('2. パスタ 🍝');
  });

  it('preserves boolean false values without dropping them', () => {
    const result = formatPollAsText({
      question: 'Q?',
      options: [{ text: 'A' }],
      is_anonymous: false,
      allows_multiple_answers: false,
    } as unknown as Poll);

    expect(result).toContain('Multiple answers: no');
    expect(result).not.toContain('Anonymous:');
  });

  it('returns undefined for null input', () => {
    expect(formatPollAsText(null as unknown as Poll)).toBeUndefined();
  });

  it('returns undefined for non-object input', () => {
    expect(formatPollAsText('not a poll' as unknown as Poll)).toBeUndefined();
  });

  it('starts with [poll] tag', () => {
    const result = formatPollAsText({
      question: 'Q?',
      options: [{ text: 'A' }],
    } as unknown as Poll);

    expect(result).toMatch(/^\[poll\] Question:/);
  });
});
