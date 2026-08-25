import { StreamEventSchema, StreamReasoningSummaryEventSchema } from '../../../../src/types/agent.types';

describe('StreamReasoningSummaryEventSchema', () => {
  it('parses a valid reasoning_summary event', () => {
    const event = { type: 'reasoning_summary', sequence: 5, text: 'Looking up your task...' };
    const result = StreamReasoningSummaryEventSchema.safeParse(event);
    expect(result.success).toBe(true);
    expect(result.data).toEqual(event);
  });

  it('rejects reasoning_summary without sequence', () => {
    const event = { type: 'reasoning_summary', text: 'Searching...' };
    const result = StreamReasoningSummaryEventSchema.safeParse(event);
    expect(result.success).toBe(false);
  });

  it('rejects reasoning_summary without text', () => {
    const event = { type: 'reasoning_summary', sequence: 5 };
    const result = StreamReasoningSummaryEventSchema.safeParse(event);
    expect(result.success).toBe(false);
  });
});

describe('StreamEventSchema discriminated union', () => {
  it('recognizes reasoning_summary type', () => {
    const event = { type: 'reasoning_summary', sequence: 1, text: 'hello' };
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
