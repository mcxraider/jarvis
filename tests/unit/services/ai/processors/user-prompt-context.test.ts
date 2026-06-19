import OpenAI from 'openai';
import { FunctionCallingProcessor } from '../../../../../src/services/ai/processors/function-calling.processor';
import { SimpleTextProcessor } from '../../../../../src/services/ai/processors/simple-text.processor';

function createOpenAI(create: jest.Mock): OpenAI {
  return {
    chat: {
      completions: {
        create,
      },
    },
  } as unknown as OpenAI;
}

describe('AI processor user prompt context', () => {
  it('adds request date and time to simple user prompts', async () => {
    const create = jest.fn().mockResolvedValue({
      choices: [{ message: { content: 'response' } }],
    });

    await new SimpleTextProcessor().processSimpleMessage(
      createOpenAI(create),
      'gpt-test',
      0.1,
      'hello',
      'user-1',
    );

    const userMessage = create.mock.calls[0][0].messages[1];
    expect(userMessage.role).toBe('user');
    expect(userMessage.content).toContain('Current request date and time:');
    expect(userMessage.content).toContain('User request:\nhello');
  });

  it('adds request date and time to function-calling user prompts', async () => {
    const create = jest.fn().mockResolvedValue({
      choices: [{ message: { content: 'response' } }],
    });

    await new FunctionCallingProcessor().processWithFunctionCalling(
      createOpenAI(create),
      'gpt-test',
      0.1,
      'add a task',
      'user-1',
    );

    const userMessage = create.mock.calls[0][0].messages[1];
    expect(userMessage.role).toBe('user');
    expect(userMessage.content).toContain('Current request date and time:');
    expect(userMessage.content).toContain('User request:\nadd a task');
  });
});
