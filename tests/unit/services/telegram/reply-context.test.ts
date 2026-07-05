import { Message } from 'telegraf/typings/core/types/typegram';
import { formatReplyContext } from '../../../../src/services/telegram/reply-context';

function asMessage(message: Record<string, unknown>): Message {
  return message as unknown as Message;
}

describe('formatReplyContext', () => {
  it('labels bot-authored text as an earlier assistant message', () => {
    const replied = asMessage({
      text: 'Created task: Buy milk',
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toBe(
      '[In reply to your earlier message: "Created task: Buy milk"]',
    );
  });

  it('recognizes the configured bot id even when is_bot is absent', () => {
    const replied = asMessage({
      text: 'Which task?',
      from: { id: 10, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toBe(
      '[In reply to your earlier message: "Which task?"]',
    );
  });

  it('labels user-authored text with the sender first name', () => {
    const replied = asMessage({
      text: 'Buy milk',
      from: { id: 22, is_bot: false, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toBe(
      '[In reply to an earlier message from Alex: "Buy milk"]',
    );
  });

  it('falls back to a photo caption', () => {
    const replied = asMessage({
      photo: [{ file_id: 'photo-1' }],
      caption: 'Receipt from lunch',
      from: { id: 22, is_bot: false, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toBe(
      '[In reply to an earlier message from Alex: "Receipt from lunch"]',
    );
  });

  it.each([
    ['no replied message', undefined],
    ['a photo without a caption', asMessage({ photo: [{ file_id: 'photo-1' }] })],
    ['blank text', asMessage({ text: ' \n\t ' })],
    ['blank caption', asMessage({ photo: [], caption: '  ' })],
  ])('returns undefined for %s', (_label, replied) => {
    expect(formatReplyContext(replied, 10)).toBeUndefined();
  });

  it('uses a generic user label when sender metadata is unavailable', () => {
    const replied = asMessage({ text: 'Earlier context' });

    expect(formatReplyContext(replied, undefined)).toBe(
      '[In reply to an earlier message from the user: "Earlier context"]',
    );
  });

  it('truncates quoted text beyond 700 characters and appends an ellipsis', () => {
    const replied = asMessage({
      text: `${'a'.repeat(700)}tail`,
      from: { id: 22, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toBe(
      `[In reply to an earlier message from Alex: "${'a'.repeat(700)}…"]`,
    );
  });

  it('does not truncate text at exactly 700 characters', () => {
    const text = 'a'.repeat(700);
    const replied = asMessage({ text, from: { id: 22, first_name: 'Alex' } });

    expect(formatReplyContext(replied, 10)).toBe(
      `[In reply to an earlier message from Alex: "${text}"]`,
    );
  });
});
