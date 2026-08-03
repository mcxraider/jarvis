import { StreamEventSchema, StreamNarrationEventSchema } from '../../../../src/types/agent.types';

describe('StreamNarrationEventSchema', () => {
  it('parses a valid narration event', () => {
    const event = { type: 'narration', sequence: 5, text: 'Looking up your task...' };
    const result = StreamNarrationEventSchema.safeParse(event);
    expect(result.success).toBe(true);
    expect(result.data).toEqual(event);
  });

  it('rejects narration without sequence', () => {
    const event = { type: 'narration', text: 'Searching...' };
    const result = StreamNarrationEventSchema.safeParse(event);
    expect(result.success).toBe(false);
  });

  it('rejects narration without text', () => {
    const event = { type: 'narration', sequence: 5 };
    const result = StreamNarrationEventSchema.safeParse(event);
    expect(result.success).toBe(false);
  });
});

describe('StreamEventSchema discriminated union', () => {
  it('recognizes narration type', () => {
    const event = { type: 'narration', sequence: 1, text: 'hello' };
    const result = StreamEventSchema.safeParse(event);
    expect(result.success).toBe(true);
  });

  it('still recognizes progress type', () => {
    const event = { type: 'progress', stage: 'lookup', message: 'Looking...' };
    const result = StreamEventSchema.safeParse(event);
    expect(result.success).toBe(true);
  });

  it('still recognizes final type', () => {
    const event = {
      type: 'final',
      response: { status: 'completed', thread_id: 't1', response: 'Done!', },
    };
    const result = StreamEventSchema.safeParse(event);
    expect(result.success).toBe(true);
  });
});
