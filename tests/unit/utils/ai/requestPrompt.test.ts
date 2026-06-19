import { buildUserPromptWithRequestDateTime } from '../../../../src/utils/ai/requestPrompt';

describe('request prompt context', () => {
  it('adds the current request date and time above the user message', () => {
    const prompt = buildUserPromptWithRequestDateTime(
      'Show me my tasks due today',
      new Date('2026-06-19T04:05:06.789Z'),
    );

    expect(prompt).toBe(
      [
        'Request context:',
        'Current request date and time: 2026-06-19T04:05:06.789Z',
        '',
        'User request:',
        'Show me my tasks due today',
      ].join('\n'),
    );
  });
});
