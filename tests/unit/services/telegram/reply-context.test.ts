import { Message } from 'telegraf/typings/core/types/typegram';
import { formatReplyContext } from '../../../../src/services/telegram/reply-context';

function asMessage(message: Record<string, unknown>): Message {
  return message as unknown as Message;
}

describe('formatReplyContext', () => {
  it('labels bot-authored text as assistant role', () => {
    const replied = asMessage({
      text: 'Created task: Buy milk',
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Created task: Buy milk',
    });
  });

  it('recognizes the configured bot id even when is_bot is absent', () => {
    const replied = asMessage({
      text: 'Which task?',
      from: { id: 10, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Which task?',
    });
  });

  it('labels user-authored text as user role', () => {
    const replied = asMessage({
      text: 'Buy milk',
      from: { id: 22, is_bot: false, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: 'Buy milk',
    });
  });

  it('falls back to a photo caption', () => {
    const replied = asMessage({
      photo: [{ file_id: 'photo-1' }],
      caption: 'Receipt from lunch',
      from: { id: 22, is_bot: false, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: 'Receipt from lunch',
    });
  });

  it.each([
    ['no replied message', undefined],
    ['a photo without a caption', asMessage({ photo: [{ file_id: 'photo-1' }] })],
    ['blank text', asMessage({ text: ' \n\t ' })],
    ['blank caption', asMessage({ photo: [], caption: '  ' })],
  ])('returns undefined for %s', (_label, replied) => {
    expect(formatReplyContext(replied, 10)).toBeUndefined();
  });

  it('uses user role when sender metadata is unavailable', () => {
    const replied = asMessage({ text: 'Earlier context' });

    expect(formatReplyContext(replied, undefined)).toEqual({
      role: 'user',
      message: 'Earlier context',
    });
  });

  it('truncates quoted text beyond 700 characters and appends an ellipsis', () => {
    const replied = asMessage({
      text: `${'a'.repeat(700)}tail`,
      from: { id: 22, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: `${'a'.repeat(700)}…`,
    });
  });

  it('does not truncate text at exactly 700 characters', () => {
    const text = 'a'.repeat(700);
    const replied = asMessage({ text, from: { id: 22, first_name: 'Alex' } });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: text,
    });
  });

  it('prefers rich_message over partial text when both exist', () => {
    const replied = asMessage({
      text: 'What would you like to edit? Here\'s what I can change:',
      rich_message: { markdown: 'Found it! The task is **"MWTS"**.\n\nWhat would you like to edit? Here\'s what I can change:\n- **Title**\n- **Due date**' },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)!.message).toContain('Found it!');
  });

  it('extracts rich_message.markdown from bot rich messages', () => {
    const replied = asMessage({
      rich_message: { markdown: 'Which dates would you like?' },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Which dates would you like?',
    });
  });

  it('extracts rich_message when it is a plain string', () => {
    const replied = asMessage({
      rich_message: 'Which dates would you like?',
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Which dates would you like?',
    });
  });

  it('extracts rich_message.text as fallback', () => {
    const replied = asMessage({
      rich_message: { text: 'Which dates would you like?' },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Which dates would you like?',
    });
  });

  it('extracts rich_message.blocks with text fields', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'paragraph', text: 'Which dates would you like?' },
          { type: 'paragraph', text: 'I can help schedule it.' },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Which dates would you like?\nI can help schedule it.',
    });
  });

  it('extracts rich_message.blocks with content arrays (inline elements)', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { content: [{ text: 'Hello ' }, { text: 'world' }] },
          { content: 'Simple string content' },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: 'Hello world\nSimple string content',
    });
  });

  it('returns undefined for rich_message.blocks with no extractable text', () => {
    const replied = asMessage({
      rich_message: { blocks: [{ type: 'image', url: 'https://...' }] },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toBeUndefined();
  });

  it('extracts list blocks with labels and items', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'paragraph', text: 'Options:' },
          {
            type: 'list',
            items: [
              { label: '•', blocks: [{ type: 'paragraph', text: 'Title' }] },
              { label: '•', blocks: [{ type: 'paragraph', text: 'Due date' }] },
              { label: '•', blocks: [{ type: 'paragraph', text: 'Priority' }] },
            ],
          },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Options:');
    expect(result.message).toContain('• Title');
    expect(result.message).toContain('• Due date');
    expect(result.message).toContain('• Priority');
  });

  it('extracts ordered list items', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'list',
          items: [
            { label: '1.', blocks: [{ type: 'paragraph', text: 'First' }] },
            { label: '2.', blocks: [{ type: 'paragraph', text: 'Second' }] },
          ],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('1. First');
    expect(result.message).toContain('2. Second');
  });

  it('extracts task-list with checkbox state', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'list',
          items: [
            { label: '•', has_checkbox: true, is_checked: true, blocks: [{ type: 'paragraph', text: 'Done' }] },
            { label: '•', has_checkbox: true, is_checked: false, blocks: [{ type: 'paragraph', text: 'Pending' }] },
          ],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('☑');
    expect(result.message).toContain('Done');
    expect(result.message).toContain('☐');
    expect(result.message).toContain('Pending');
  });

  it('extracts paragraph + divider + paragraph + lists (real log dump)', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'paragraph', text: 'Above the divider.' },
          { type: 'divider' },
          { type: 'paragraph', text: 'Below the divider.' },
          { type: 'list', items: [
            { label: '•', blocks: [{ type: 'paragraph', text: 'Item A' }] },
            { label: '•', blocks: [{ type: 'paragraph', text: 'Item B' }] },
          ]},
          { type: 'list', items: [
            { label: '•', blocks: [{ type: 'paragraph', text: 'Item C' }] },
          ]},
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Above the divider.');
    expect(result.message).toContain('Below the divider.');
    expect(result.message).toContain('• Item A');
    expect(result.message).toContain('• Item B');
    expect(result.message).toContain('• Item C');
  });

  it('extracts heading + details with nested list', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'heading', text: 'Edit options' },
          {
            type: 'details',
            summary: 'Details heading',
            blocks: [
              { type: 'list', items: [
                { label: '•', blocks: [{ type: 'paragraph', text: 'Nested item' }] },
              ]},
            ],
          },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Edit options');
    expect(result.message).toContain('Details heading');
    expect(result.message).toContain('• Nested item');
  });

  it('extracts table cells', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'table',
          cells: [
            [{ text: 'Name', is_header: true }, { text: 'Value', is_header: true }],
            [{ text: 'Priority' }, { text: 'P1' }],
          ],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Name | Value');
    expect(result.message).toContain('Priority | P1');
  });

  it('handles inline array text (bold/italic inline runs)', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'paragraph',
          text: ['List item with ', { type: 'italic', text: 'italic' }],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('List item with italic');
  });

  it('handles inline object text (bare bold wrapper)', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'paragraph',
          text: { type: 'bold', text: ['Hello ', { type: 'italic', text: 'world' }] },
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Hello world');
  });

  it('handles deeply nested inline runs', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'paragraph',
          text: { type: 'bold', text: { type: 'italic', text: { type: 'underline', text: 'deep' } } },
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('deep');
  });

  it('extracts blockquote blocks', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'blockquote',
          blocks: [{ type: 'paragraph', text: 'Quoted text' }],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Quoted text');
  });

  it('extracts footer blocks', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'paragraph', text: 'Main content' },
          { type: 'footer', text: 'Footnote here' },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('Main content');
    expect(result.message).toContain('Footnote here');
  });

  it('handles mathematical_expression inline and ignores anchor', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'paragraph',
          text: ['E=', { type: 'mathematical_expression', expression: 'mc²' }, { type: 'anchor', name: 'ref1' }],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('E=mc²');
  });

  it('handles table cells with array/object text', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [{
          type: 'table',
          cells: [
            [{ text: [{ type: 'bold', text: '42' }, ' ', { type: 'superscript', text: 'ms' }] }],
            [{ text: { type: 'spoiler', text: 'ready' } }],
          ],
        }],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result.message).toContain('42 ms');
    expect(result.message).toContain('ready');
  });

  it('extracts full nested syntax mega-message', () => {
    const replied = asMessage({
      rich_message: {
        blocks: [
          { type: 'heading', text: 'Task Summary' },
          { type: 'paragraph', text: 'Here are your tasks.' },
          { type: 'blockquote', blocks: [{ type: 'paragraph', text: 'Important note' }] },
          { type: 'list', items: [
            { label: '•', blocks: [{ type: 'paragraph', text: ['Buy ', { type: 'bold', text: 'milk' }] }] },
          ]},
          { type: 'table', cells: [[{ text: 'Status' }, { text: 'Done' }]] },
          { type: 'footer', text: 'Last updated today' },
        ],
      },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    const result = formatReplyContext(replied, 10)!;
    expect(result).toBeDefined();
    expect(result.message).toContain('Task Summary');
    expect(result.message).toContain('Here are your tasks.');
    expect(result.message).toContain('Important note');
    expect(result.message).toContain('Buy milk');
    expect(result.message).toContain('Status | Done');
    expect(result.message).toContain('Last updated today');
  });

  it('extracts poll question as fallback', () => {
    const replied = asMessage({
      poll: { question: 'Where should we eat?' },
      from: { id: 22, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: '[Poll: Where should we eat?]',
    });
  });

  it('extracts sticker emoji as fallback', () => {
    const replied = asMessage({
      sticker: { emoji: '👍', file_id: 'sticker-1' },
      from: { id: 10, is_bot: true, first_name: 'Jarvis' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'assistant',
      message: '[Sticker: 👍]',
    });
  });

  it('extracts contact name as fallback', () => {
    const replied = asMessage({
      contact: { first_name: 'John', phone_number: '+1234' },
      from: { id: 22, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: '[Contact: John]',
    });
  });

  it('extracts location as fallback', () => {
    const replied = asMessage({
      location: { latitude: 25.0, longitude: 121.5 },
      from: { id: 22, first_name: 'Alex' },
    });

    expect(formatReplyContext(replied, 10)).toEqual({
      role: 'user',
      message: '[Shared location]',
    });
  });
});
